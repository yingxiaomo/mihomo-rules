#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
train_pld.py — PLD 先验模型训练器（Predictive-LightGBM & Discounted-UCB 阶段一）

相关仓库：
  - 魔改内核（配套）：https://github.com/yingxiaomo/mihomo （dev-smart 分支）
  - 本训练仓库：https://github.com/yingxiaomo/mihomo-rules

作用：训练一个 LightGBM「先验」模型——给定请求侧特征（目标 ASN、时段、
协议、域名/地区/端口类别等）与节点辅助信号，预测该节点对本次请求的基础
得分（Stage-1 先验分）。该分数在选路时刻被内核的
component/smart/lightgbm.PriorModel 使用，与在线 reward EMA 及探索项融合
成 D-UCB 最终权重（Stage-2）。

特征顺序必须与内核严格一致：
  component/smart/lightgbm/prior.go  ->  preparePriorFeatures()  （30 特征，
  其中 15-19 位为节点实时/语义特征：node_delay / node_reward_var /
  target_hash / node_type / group_hash）

标签：使用内核 collector 写入的 `reward` 列（Stage-3 反馈折算值），而非
旧版 `weight` 列。reward 越高代表该连接实际表现越好。节点侧 reward EMA /
样本数特征按 (target, node) 分组计算，与内核 (group, config, target, node)
统计键对齐；旧版 CSV 无 target 列时回退为按 node 分组。模型文件末尾附加的
[transforms] 段（node_sample_count 的 RobustScaler）由内核 PriorModel 在
推理时应用。

样本权重 `sample_weight`：内核（dev-smart 双通道版）collector 对每条连接
写入按下载量对数计算的权重（512KiB→1.0，5MB→3.5，100MB→7.7，1GB→11.0），
使「大流量 = 黄金数据」真正体现在 LightGBM 拟合中，网页小流量不再以
数量淹没大流量样本。旧数据/旧 CSV 无该列时自动等权 1.0。

双通道标签：内核 PLDRecord 现在按流量拆两个独立 reward 通道（小流量延迟
通道 / 大流量吞吐通道），collector 写入的 reward 是本次连接所属通道的
EMA。模型特征中的 domain_traffic_level / traffic_class 让先验在同一空间
区分「大流量场景谁快 / 小流量场景谁稳」。

本脚本与 train.py（官方 30 特征、weight 标签训练器）并存：
  - train.py       -> 官方版内核（uselightgbm 链路），发布 smart-model
  - train_pld.py   -> 魔改内核（usepld/PLD 链路），发布 smart-pld-model
两者特征 schema 不同，请勿混用。
"""

import argparse
import glob
import logging
import os
import sys
import time
import traceback
import warnings
import ipaddress
import re
from pathlib import Path
from typing import List

import requests

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=".*X does not have valid feature names.*")
warnings.filterwarnings("ignore", category=UserWarning)

import lightgbm as lgb
from sklearn.metrics import mean_absolute_error
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler, RobustScaler

logger = logging.getLogger(__name__)


# ==============================================================================
# PLD 特征定义 —— 与内核 component/smart/lightgbm/prior.go 严格对齐
# ==============================================================================
# preparePriorFeatures 输出 30 个特征，idx 见内核注释。训练样本必须按同一顺序
# 构造，否则 PriorModel.PredictPrior 推理错位。
PLD_FEATURE_ORDER: List[str] = [
    # 请求侧 (10)
    "asn_feature",        # 0  extractASNFeature(DestASN)
    "is_udp",             # 1  protocol
    "hour_sin",           # 2  cyclic hour
    "hour_cos",           # 3  cyclic hour
    "is_peak",            # 4  20:00-24:00 & 00:00-01:00
    "domain_feature",     # 5  extractDomainTypeFeature(host)
    "geo_feature",        # 6  extractGeoIPFeature(geoip)
    "port_feature",       # 7  extractPortFeature(port)
    "traffic_class",      # 8  ClassifyTrafficFromMetadata
    "asn_hash",           # 9  FNV-1a(DestASN) % 500 + 1
    "host_hash",          # 10 FNV-1a(host) % 1000 + 1
    "ip_hash",            # 11 FNV-1a(ip) % 10000 + 1
    # 节点侧 (3)
    "node_reward_ema",    # 12 node EWMA reward (clamped 0..1)
    "node_sample_count",  # 13 log1p(sample count)
    "node_hash",          # 14 FNV-1a(nodeName) % 1000 + 1
    # 节点实时/语义 (5) —— 与内核 prior.go feature 15-19 对齐
    "node_delay",         # 15 最近 url-test 延迟 log1p(ms/100)，0=未知
    "node_reward_var",    # 16 reward 方差(clamp 0..2 /2)，不确定性/UCB 语义
    "target_hash",        # 17 FNV-1a(SmartTarget) % 1000 + 1，站点集群
    "node_type",          # 18 AdapterType 枚举值（协议类型）
    "group_hash",         # 19 FNV-1a(groupName) % 200 + 1，策略组偏好
    "domain_traffic_level",  # 20 域名流量档位 0=unknown 1=small 2=medium 3=heavy
    # 保留 (9) —— 内核固定 0 填充
] + [f"reserved_{i}" for i in range(21, 30)]

assert len(PLD_FEATURE_ORDER) == 30, f"PLD feature order must be 30, got {len(PLD_FEATURE_ORDER)}"

# ---------------------------------------------------------------------------
# 域名流量档位（feature 20）—— 与内核 traffic_profile.go 严格对齐
# ---------------------------------------------------------------------------
# 阈值：平均单次下载 < 512KiB → small；>= 512KiB → medium；>= 4MiB → heavy；
# 历史峰值速率 >= 8MB/s 也可判 heavy。download_mb 单位为 MiB。
TRAFFIC_SMALL_MB = 0.5    # 512 KiB
TRAFFIC_HEAVY_MB = 4.0    # 4 MiB
TRAFFIC_HEAVY_PEAK_KBPS = 8192.0

# 内置「域名后缀 → 档位」表（与内核 builtinTrafficTable 同源）。
# 只收录高置信条目：纯视频 CDN 子域 → heavy(3)；混合/音频/图片 CDN → medium(2)。
# 应用壳域名（youtube.com / netflix.com 本身）刻意不在表内 —— 靠实测画像兜底。
BUILTIN_TRAFFIC_TABLE = [
    # heavy：纯视频流域名
    (".googlevideo.com", 3),  # YouTube 视频流
    (".gvt1.com", 3),         # YouTube 视频边缘节点
    (".bilivideo.com", 3),    # B 站视频流
    (".nflxvideo.net", 3),    # Netflix 视频流
    (".hulustream.com", 3),   # Hulu 视频流
    (".ttvnw.net", 3),        # Twitch 视频（hls/vod）
    (".vhcdn.net", 3),        # 虎牙直播流
    # medium：混合型 / 音频 / 图片 CDN
    (".akamaized.net", 2),    # 混合（Netflix 视频 + 网站图片）
    (".bmcdn.net", 2),        # B 站 CDN（视频+图片混合）
    (".cloudfront.net", 2),   # AWS 混合 CDN
    (".scdn.co", 2),          # Spotify 音频
    (".tiktokcdn.com", 2),    # TikTok 混合
    (".bytecdn.cn", 2),       # 抖音混合
    (".gtimg.com", 2),        # 腾讯混合 CDN
    (".iqiyipic.com", 2),     # 爱奇艺图片
    (".ykimg.com", 2),        # 优酷图片
]


def builtin_traffic_level(host: str) -> int:
    """内置表命中返回档位（3/2），未命中返回 0。"""
    if not host:
        return 0
    h = str(host).strip().lower()
    for suffix, level in BUILTIN_TRAFFIC_TABLE:
        if h.endswith(suffix):
            return level
    return 0


def traffic_level_from_avg(avg_download_mb: float, peak_kbps: float) -> int:
    """按 target 聚合画像计算档位（与内核 UpdateTargetTrafficLevel 阈值一致）。"""
    if avg_download_mb >= TRAFFIC_HEAVY_MB or peak_kbps >= TRAFFIC_HEAVY_PEAK_KBPS:
        return 3
    if avg_download_mb >= TRAFFIC_SMALL_MB:
        return 2
    return 1

# ---------------------------------------------------------------------------
# 与内核一致的 FNV-1a 哈希 (lightgbm.go:hashStringToFloat)
# ---------------------------------------------------------------------------
def fnv1a_hash(s: str, buckets: int) -> float:
    if not s or buckets <= 0:
        return 0.0
    h = 2166136261  # fnvOffsetBasis
    prime = 16777619  # fnvPrime
    for ch in s:
        h = (h ^ ord(ch)) & 0xFFFFFFFF
        h = (h * prime) & 0xFFFFFFFF
    return float((h % buckets) + 1)


# ---------------------------------------------------------------------------
# 与内核一致的 extractASNFeature 映射配置 (lightgbm.go:asnCategories)
# ---------------------------------------------------------------------------
ASN_CATEGORIES = {
    # 全球科技
    "google":     1,
    "amazon":     2,
    "microsoft":  3,
    "facebook":   4,
    "apple":      5,
    "cloudflare": 6,
    "akamai":     7,
    "fastly":     8,
    "netflix":    9,
    "alibaba":    10,
    "tencent":    11,
    "baidu":      12,
    # 中国运营商
    "chinatelecom": 13,
    "chinaunicom":  14,
    "chinamobile":  15,
    "chinaedu":     16,
    "cstnet":       17,
    # 全球CDN/云服务
    "cdn77":        20,
    "limelight":    21,
    "edgecast":     22,
    "stackpath":    23,
    "imperva":      24,
    "oracle":       25,
    "ibm":          26,
    "digitalocean": 27,
    "linode":       28,
    "ovh":          29,
    "hetzner":      30,
    "vultr":        31,
    "cogent":       32,
    "leaseweb":     33,
    "upyun":        34,
    "qingcloud":    35,
    "ucloud":       36,
    # 国际主要运营商
    "verizon":  40,
    "comcast":  41,
    "att":      42,
    "sprint":   43,
    "tmobile":  44,
    "level3":   45,
    "ntt":      46,
    "kddi":     47,
    "softbank": 48,
    "telstra":  49,
    "singtel":  50,
    "starhub":  51,
    "m1":       52,
    "pccw":     53,
    "hkbn":     54,
    "smartone": 55,
    "hgc":      56,
    "cht":      57,
    "fetnet":   58,
    "twm":      59,
    # 内容提供商
    "twitter":    70,
    "twitch":     71,
    "discord":    72,
    "spotify":    73,
    "github":     74,
    "steam":      75,
    "blizzard":   76,
    "riotgames":  77,
    "epicgames":  78,
    "ea":         79,
    "bytedance":  80,
    "bilibili":   81,
    "netactuate": 82,
    # 主要交换中心
    "hkix":    90,
    "linx":    91,
    "jpix":    92,
    "equinix": 93,
    "sgix":    94,
    "de-cix":  95,
    "ams-ix":  96,
    # 教育科研
    "cern":     100,
    "mit":      101,
    "stanford": 102,
    "tsinghua": 103,
    "pku":      104,
    # 金融行业
    "visa":       110,
    "mastercard": 111,
    "paypal":     112,
    "stripe":     113,
    "alipay":     114,
    "wechatpay":  115,
}

def extract_asn_feature(asn_raw: str) -> int:
    if not asn_raw or asn_raw == "unknown":
        return 0
    low = asn_raw.lower()

    # 1. 检查是否匹配已知ASN类别
    for keyword, category in ASN_CATEGORIES.items():
        if keyword in low:
            return category

    # 2. 尝试提取ASN号码并分类
    s = low
    if s.startswith("as") and len(s) > 2:
        s = s[2:]

    digits = []
    for ch in s:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    if digits:
        try:
            asn_num = int("".join(digits[:8]))
            if asn_num < 1000:
                return 50
            elif asn_num < 10000:
                return 51
            elif asn_num < 50000:
                return 52
            elif asn_num < 150000:
                return 53
            else:
                return 54
        except ValueError:
            pass
    return 0


# ---------------------------------------------------------------------------
# 与内核一致的 extractGeoIPFeature 映射配置 (lightgbm.go:geoCategories)
# ---------------------------------------------------------------------------
GEO_CATEGORIES = {
    "CN": 1,  # 中国
    "HK": 2,  # 香港
    "TW": 3,  # 台湾
    "JP": 4,  # 日本
    "KR": 5,  # 韩国
    "SG": 6,  # 新加坡
    "US": 7,  # 美国
    "CA": 8,  # 加拿大
    "GB": 9,  # 英国
    "DE": 10, # 德国
    "FR": 11, # 法国
    "RU": 12, # 俄罗斯
    "AU": 13, # 澳大利亚
    "IN": 14, # 印度
    "BR": 15, # 巴西
    "IT": 16, # 意大利
    "ES": 17, # 西班牙
    "NL": 18, # 荷兰
    "SE": 19, # 瑞典
    "CH": 20, # 瑞士
    "PL": 21, # 波兰
    "TR": 22, # 土耳其
    "MX": 23, # 墨西哥
    "ZA": 24, # 南非
    "AR": 25, # 归根阿根廷
    "ID": 26, # 印度尼西亚
    "TH": 27, # 泰国
    "VN": 28, # 越南
    "PH": 29, # 菲律宾
    "MY": 30, # 马来西亚
    "MO": 31, # 澳门
}

def extract_geo_feature(geoip: str) -> int:
    if not geoip or geoip == "unknown":
        return 0
    g = geoip.upper().strip()
    if g in GEO_CATEGORIES:
        return GEO_CATEGORIES[g]

    # 其他地区使用简单哈希分类
    hash_value = 0
    for char in g:
        hash_value = hash_value * 31 + ord(char)
    return 30 + (hash_value % 20)


# ---------------------------------------------------------------------------
# 与内核一致的域名类别提取配置
# ---------------------------------------------------------------------------
DNS_SERVICE_KEYWORDS = [
    "8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1", "9.9.9.9", "149.112.112.112",
    "208.67.222.222", "208.67.220.220",
    "dns.google", "dns.google.com", "cloudflare-dns.com", "dns.cloudflare.com",
    "one.one.one.one", "family.cloudflare-dns.com", "security.cloudflare-dns.com",
    "dns.quad9.net", "dns9.quad9.net", "dns10.quad9.net", "dns11.quad9.net",
    "doh.opendns.com", "doh.familyshield.opendns.com", "doh.sandbox.opendns.com",
    "mozilla.cloudflare-dns.com", "firefox.dns.nextdns.io",
    "dns.adguard.com", "dns-family.adguard.com", "dns-unfiltered.adguard.com",
    "doh.cleanbrowsing.org", "family-filter-dns.cleanbrowsing.org",
    "dot.cloudflare-dns.com", "dot.alidns.com", "dot.dns.sb", "dot.360.cn",
    "doh.pub", "dns.pub", "doh.360.cn", "dns.alidns.com", "doh.alidns.com",
    "doh.dns.sb", "rubyfish.cn", "dns.rubyfish.cn", "pdns.fkgfw.cf",
    "commons.host", "odvr.nic.cz", "doh.libredns.gr", "dns.digitale-gesellschaft.ch",
    "dns.switch.ch", "jp.tiar.app", "jp.tiarap.org", "kaitain.restena.lu",
    "dns.twnic.tw", "dns.hinet.net",
    "dns", "doh", "doq", "dot", "resolver", "nameserver", "recursive",
    "authoritative", "secure-dns", "private-dns",
]

API_SERVICE_KEYWORDS = [
    "api.cloudflare.com", "api.amazonaws.com", "api.azure.com", "googleapis.com",
    "api.fastly.com", "api.maxcdn.com", "api.keycdn.com", "api.bunnycdn.com",
    "api.digitalocean.com", "api.vultr.com", "api.linode.com", "api.hetzner.com",
    "api.vercel.com", "api.netlify.com", "api.heroku.com", "api.railway.app",
    "api.render.com", "api.fly.io", "registry.npmjs.org", "pypi.org",
    "hub.docker.com", "registry.docker.io", "rubygems.org", "crates.io",
    "api.datadog.com", "api.newrelic.com", "api.segment.com", "api.mixpanel.com",
    "api.amplitude.com", "api.hotjar.com", "api.sentry.io", "api.rollbar.com",
    "api.auth0.com", "api.okta.com", "api.twilio.com", "api.sendgrid.com",
    "api.mailgun.com", "api.stripe.com",
    "ecs.aliyuncs.com", "api.qcloud.com", "api.ucloud.cn", "api.huaweicloud.com",
    "api.baidubce.com", "api.volcengine.com",
    "gateway.", "api-gateway.", "apigateway.", "/api/", "/v1/", "/v2/", "/v3/", "/v4/",
    "/rest/", "/graphql/", "rest.", "graphql.", "webhook.", "rpc.",
]

GAME_KEYWORDS = [
    "game", "play", "steam", "xbox", "playstation", "nintendo", "ea.com", "riot",
    "blizzard", "ubisoft", "epic", "cod", "minecraft", "roblox", "pubg", "fortnite",
    "valorant", "riotgames", "leagueoflegends", "warzone",
    "apex", "apexlegends", "overwatch", "dota", "csgo",
    "counterstrike", "hearthstone", "battlenet", "battle.net",
    "genshin", "mihoyo", "hoyoverse", "lol", "arenaofvalor", "honorofkings",
]

COMMUNICATION_KEYWORDS = [
    "meet", "zoom", "teams", "voip", "sip", "call", "chat", "conference", "webex",
    "discord", "slack", "telegram", "signal", "whatsapp", "skype", "wechat",
    "voicechat", "videocall", "rtc", "webrtc", "jitsi",
    "mumble", "ventrilo", "teamspeak", "discord.gg",
    "meeting", "conference", "huddle", "gather",
    "qq", "msn", "icq", "line", "kakao", "viber", "imo", "element",
]

STREAMING_KEYWORDS = [
    "youtube", "netflix", "hulu", "spotify", "tiktok", "douyin", "youku", "iqiyi",
    "bilibili", "twitch", "hbo", "disney", "vimeo", "vod", "stream", "video",
    "media", "movie", "tv", "music", "audio", "cdm", "cdn", "content",
    "live", "livestream", "replay", "shorts", "kuaishou", "huya", "douyu",
]

# 构建带优先级的列表
ALL_DOMAIN_KEYWORDS = []
for k in DNS_SERVICE_KEYWORDS:
    ALL_DOMAIN_KEYWORDS.append((k, 6))
for k in API_SERVICE_KEYWORDS:
    ALL_DOMAIN_KEYWORDS.append((k, 5))
for k in GAME_KEYWORDS:
    ALL_DOMAIN_KEYWORDS.append((k, 3))
for k in COMMUNICATION_KEYWORDS:
    ALL_DOMAIN_KEYWORDS.append((k, 4))
for k in STREAMING_KEYWORDS:
    ALL_DOMAIN_KEYWORDS.append((k, 2))

# ---------------------------------------------------------------------------
# 与内核一致的 IP 分类配置 (lightgbm.go:privateIPNetworks)
# ---------------------------------------------------------------------------
PRIVATE_NETWORKS = [
    (ipaddress.ip_network("10.0.0.0/8"), 101),
    (ipaddress.ip_network("172.16.0.0/12"), 101),
    (ipaddress.ip_network("192.168.0.0/16"), 101),
    (ipaddress.ip_network("127.0.0.0/8"), 102),
    (ipaddress.ip_network("169.254.0.0/16"), 103),
    (ipaddress.ip_network("::1/128"), 102),
    (ipaddress.ip_network("fe80::/10"), 103),
    (ipaddress.ip_network("fc00::/7"), 101),
    (ipaddress.ip_network("2001:db8::/32"), 104),
]

def extract_ip_feature(ip_str: str) -> int:
    if not ip_str or ip_str == "unknown":
        return 0
    ip_str = ip_str.strip("[]")
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return 0

    for net, cat in PRIVATE_NETWORKS:
        if addr in net:
            return cat

    if addr.version == 4:
        return 110  # IPv4公网地址
    else:
        return 111  # IPv6公网地址


def extract_domain_feature(host: str) -> int:
    if not host or host == "unknown":
        return 0
    host = host.lower()

    # 1. 检查是否为IP地址形式
    if host.startswith("["):
        return 1
    try:
        ipaddress.ip_address(host)
        return 1
    except ValueError:
        pass

    # 2. 关键词匹配（优先级：DNS > API > 游戏 > 通信 > 流媒体）
    for kw, cat in ALL_DOMAIN_KEYWORDS:
        if kw in host:
            return cat

    # 3. 顶级域名检查
    if host.endswith(".gov"):
        return 14
    elif host.endswith(".edu"):
        return 15
    elif host.endswith(".cn"):
        return 10
    elif host.endswith(".com"):
        return 11
    elif host.endswith(".net"):
        return 12
    elif host.endswith(".org"):
        return 13

    # 4. 域名结构分析
    parts = host.split(".")
    if len(parts) >= 3:
        return 30
    else:
        return 31


# ---------------------------------------------------------------------------
# 与内核一致的 Port 分类配置
# ---------------------------------------------------------------------------
WELL_KNOWN_PORTS = {
    22:    1,  # SSH
    25:    2,  # SMTP
    53:    3,  # DNS
    80:    4,  # HTTP
    110:   5,  # POP3
    143:   6,  # IMAP
    443:   7,  # HTTPS
    465:   8,  # SMTPS
    784:   3,  # DoQ
    853:   3,  # DoT
    993:   9,  # IMAPS
    995:   10, # POP3S
    1194:  11, # OpenVPN
    1812:  12, # RADIUS
    3306:  13, # MySQL
    5053:  3,  # DNS备用
    5353:  3,  # mDNS
    5355:  3,  # LLMNR
    5432:  14, # PostgreSQL
    6379:  15, # Redis
    8853:  3,  # DoT备用
    9953:  3,  # DNS管理
    27017: 16, # MongoDB
    6660:  17, # IRC
    6665:  17,
    6666:  17,
    6667:  17,
    6668:  17,
    6669:  17,
    8000:  18, # 替代HTTP
    8008:  18,
    8080:  18,
    8443:  19, # 替代HTTPS
    8883:  20, # MQTT over TLS
}

PORT_RANGES = [
    (0, 1023, 20),      # 系统端口
    (1024, 49151, 21),  # 注册端口
    (49152, 65535, 22), # 动态端口
]

API_SERVICE_PORTS = {
    8080, 8443, 9000, 9001, 9002,
    3000, 3001, 5000, 5001,
    8000, 8001, 8888, 4000, 4001,
    6000, 6001, 7000, 7001,
}

DNS_SERVICE_PORTS = {
    53, 853, 784, 5053, 5353, 5355, 8853, 9953
}

GAME_SPECIFIC_PORTS = {
    25565,
    27015, 27016, 27017, 27018, 27019, 27020,
    27031, 27036,
    3074,
    3478, 3479,
    3659,
    6250,
    7000, 7001, 7002, 7003, 7004,
    8393, 8394,
    9000, 9001,
    9330, 9331,
    9339,
    14000, 14001, 14002, 14003, 14004, 14008,
    16000,
    18000, 18060, 18120, 18180, 18240, 18300,
    19000, 19132,
    20000, 20001, 20002,
    22100, 22101, 22102,
    30000, 30001, 30002, 30003, 30004,
    35000, 35001, 35002,
    40000, 40001, 40002,
    50000, 50001, 50002,
    50505,
    65010, 65050,
    3724, 6112, 6881,
}

COMMUNICATION_PORTS = {
    5060, 5061,
    1720,
    1080, 1443,
    3478, 3479,
    5349, 5350,
    5222, 5269,
    5938,
    6881, 6882, 6883, 6884, 6885, 6886, 6887, 6888, 6889,
    8801, 8802,
    8443,
    10000, 10001,
    19302, 19303,
    50000, 50001, 50002, 50003, 50004, 50005,
    55000, 55001,
    1863,
    5228,
    34784,
}

GAME_COMM_RANGES = [
    (3000, 3999, 3),   # 混合
    (5000, 5999, 2),   # 通信
    (6000, 7000, 3),   # 混合
    (8000, 9000, 3),   # 混合
    (10000, 20000, 3), # 混合
    (27000, 28000, 1), # 游戏
    (30000, 32000, 1), # 游戏
    (49000, 50000, 2), # 通信
    (50000, 55000, 3), # 混合
    (55000, 60000, 2), # 通信
]

def extract_port_feature(port) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        return 0

    # 1.1 DNS服务端口
    if p in DNS_SERVICE_PORTS:
        return 36

    # 1.2 API服务端口
    if p in API_SERVICE_PORTS:
        return 35

    # 1.3 游戏专用端口
    if p in GAME_SPECIFIC_PORTS:
        return 30

    # 1.4 通信专用端口
    if p in COMMUNICATION_PORTS:
        return 31

    # 2. 已知标准端口检查
    if p in WELL_KNOWN_PORTS:
        return WELL_KNOWN_PORTS[p]

    # 3.1 游戏/通信端口范围
    for r_min, r_max, cat in GAME_COMM_RANGES:
        if r_min <= p <= r_max:
            if cat == 1:
                return 32
            elif cat == 2:
                return 33
            elif cat == 3:
                return 34

    # 3.2 通用端口范围
    for r_min, r_max, cat in PORT_RANGES:
        if r_min <= p <= r_max:
            return cat

    return 0


def classify_traffic(is_udp: bool, port) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        return 0
    pf = extract_port_feature(p)
    if is_udp:
        if 30 <= pf < 35:
            return 3
        return 1
    if p == 443:
        return 0
    if p <= 1024 and pf < 20:
        return 2
    return 0


def hour_features(ts_str: str):
    """Parse RFC3339 timestamp -> (hour_sin, hour_cos, is_peak)."""
    h = 12.0
    try:
        t = pd.Timestamp(ts_str)
        if pd.isna(t):
            h = 12.0  # NaN/NaT timestamp -> neutral noon
        else:
            h = t.hour + t.minute / 60.0
    except Exception:
        pass
    import math
    ang = h / 24.0 * 2.0 * math.pi

    # 对齐 Go 端：hour 采用整数判定 (BuildRequestFeatures 里是 now.Hour())
    h_int = int(h)
    is_peak = 1.0 if (h_int >= 20 or h_int < 1) else 0.0
    return math.sin(ang), math.cos(ang), is_peak


# ---------------------------------------------------------------------------
# 路径/默认配置
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
# 输出 PriorModel.bin：PLD 先验模型独立于官方 weight 模型（Model.bin）。
# 两者特征顺序不同（PLD 请求侧 30 特征 vs 官方事后统计 30 特征），
# 必须分开存放，内核 GetPriorModel 从 PriorModel.bin 加载。
DEFAULT_MODEL_PATH = PROJECT_ROOT / "PriorModel.bin"

LGBM_PARAMS = {
    "objective": "regression",
    "metric": "rmse",
    "n_estimators": 10000,
    "learning_rate": 0.01,
    "random_state": 42,
    "n_jobs": -1,
    "verbosity": -1,
    "num_leaves": 96,
    "max_depth": 12,
    "min_data_in_leaf": 30,
    "feature_fraction": 0.85,
    "bagging_fraction": 0.85,
    "bagging_freq": 5,
}
EARLY_STOPPING_ROUNDS = 150

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
LOG_FILENAME = SCRIPT_DIR / "training_pld.log"

# RobustScaler used for the two volatile categorical-ish ints in the legacy
# pipeline; for PLD we keep it minimal to stay robust on noisy reward labels.
ROBUST_SCALER_FEATURES = ["node_sample_count"]

MAX_TG_CHUNKS = 5  # Telegram 日志最大发送块数


# ==============================================================================
# 日志与 Telegram 通知（与 train.py 同款：详细日志 + 分块推送）
# ==============================================================================
def send_telegram_msg(text: str) -> None:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=data, timeout=10)
    except Exception as e:
        print(f"⚠️ TG 发送失败: {e}")


def send_telegram_logs(header_msg: str) -> None:
    """推送训练日志到 Telegram（分块，最多 MAX_TG_CHUNKS 块）。"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，已跳过消息推送")
        return

    send_telegram_msg(header_msg)

    try:
        with open(LOG_FILENAME, "r", encoding="utf-8") as f:
            content = f.read()
        print(f"📋 日志文件大小: {len(content)} 字符")
    except Exception as e:
        content = f"无法读取日志文件: {e}"
        print(f"⚠️ 读取日志文件失败: {e}")

    chunk_size = 3500
    total_len = len(content)

    if total_len == 0:
        send_telegram_msg("<i>(日志为空)</i>")
        return

    chunks = [content[i : i + chunk_size] for i in range(0, total_len, chunk_size)]

    if len(chunks) > MAX_TG_CHUNKS:
        chunks = chunks[:MAX_TG_CHUNKS]
        chunks[-1] = chunks[-1][:-50] + "... (日志过长，已截断)"

    for i, chunk in enumerate(chunks):
        formatted_msg = f"<pre>{chunk}</pre>"
        send_telegram_msg(formatted_msg)
        if i < len(chunks) - 1:
            time.sleep(0.5)

    print(f"📨 Telegram 日志已推送 ({len(chunks)}/{min(len(chunks), MAX_TG_CHUNKS)} 块)")


def print_separator(title: str = None) -> None:
    logger.info("=" * 60)
    if title:
        logger.info(f"{title}")
        logger.info("=" * 60)


def check_data_balance(df: pd.DataFrame, top_ratio_threshold: float = 0.5, min_nodes: int = 5) -> bool:
    """
    节点分布健康检查：识别反馈循环/选择偏差风险（与 train.py 同款）。

    PLD 的 D-UCB 会偏好 reward 高的节点 -> 被偏好节点产生更多样本 ->
    数据自我强化当前偏好（"训练节点永远选不到"的统计版）。健康数据应覆盖
    足够多的节点、且没有单一节点独占。此检查在训练前跑，异常时告警但不阻断。
    """
    if df is None or len(df) == 0:
        logger.info("ℹ️ 无数据，跳过节点分布检查")
        return False

    if "node_name" not in df.columns:
        logger.info("ℹ️ 数据缺少 node_name 列，跳过节点分布检查")
        return True

    counts = df["node_name"].value_counts()
    total = len(df)
    coverage = len(counts)
    top_node = counts.index[0]
    top_ratio = counts.iloc[0] / total
    top5_ratio = counts.head(5).sum() / total

    logger.info("📊 [节点分布检查]")
    logger.info(f"  覆盖节点数: {coverage} | 总样本: {total}")
    logger.info(f"  单节点最大占比: {top_ratio:.1%} ({top_node})")
    logger.info(f"  前5节点合计占比: {top5_ratio:.1%}")

    warnings = []
    if coverage < min_nodes:
        warnings.append(f"覆盖节点过少 ({coverage}<{min_nodes})，样本代表性不足")
    if top_ratio > top_ratio_threshold:
        warnings.append(
            f"单节点占比 {top_ratio:.1%} > {top_ratio_threshold:.0%}，疑似反馈循环："
            "被模型偏好的节点自我强化，其他节点几乎无样本。建议提高 explore-strength、"
            "多攒几天数据，或先做节点均衡采样再训"
        )
    if top5_ratio > 0.9:
        warnings.append("前5节点合计占比 >90%，节点多样性不足")

    if warnings:
        logger.warning("⚠️ 节点分布健康告警（反馈循环风险）:")
        for w in warnings:
            logger.warning(f"  - {w}")
        logger.info("💡 若本批次数据来自长期运行，请斟酌是否用此数据训练")
        return False
    logger.info("✅ 节点分布健康，可直接训练")
    return True


# ==============================================================================
# 数据加载与特征构造
# ==============================================================================
def load_data(data_dir: Path) -> pd.DataFrame:
    logger.info("[步骤1] 加载原始数据")
    if not data_dir.exists():
        raise FileNotFoundError(f"数据目录不存在: {data_dir}")

    import sqlite3

    # 只收 PLD 数据：smart_samples*.csv（PLD CSV 回退）+ smart_samples*.db（PLD SQLite）。
    # 官方 smart_weight_data.csv（40 列，缺 reward/target/node_delay/node_type）不参与
    # PLD 训练，绝不能混入 data_dir —— 故这里按文件名白名单 + 必需列校验双重隔离。
    csv_files = sorted(glob.glob(str(data_dir / "smart_samples*.csv")))
    db_files = sorted(glob.glob(str(data_dir / "smart_samples*.db")))
    if not csv_files and not db_files:
        raise FileNotFoundError(f"找不到 smart_samples*.csv / smart_samples*.db: {data_dir}")

    dfs = []
    if csv_files:
        logger.info(f"📥 选中 {len(csv_files)} 个 CSV（smart_samples*.csv，已排除官方 smart_weight_data.csv）")
        for f in csv_files:
            try:
                df = pd.read_csv(f, encoding="utf-8", on_bad_lines="skip")
                dfs.append(df)
            except Exception as e:
                logger.info(f"⚠️ 跳过 {f}: {e}")
                continue

    if db_files:
        logger.info(f"📥 选中 {len(db_files)} 个 SQLite")
        for f in db_files:
            try:
                conn = sqlite3.connect(f)
                df = pd.read_sql("SELECT * FROM samples", conn)
                conn.close()
                # SQLite 的 ts 是 unix 秒 → 归一化成与 CSV 一致的 RFC3339 字符串
                if "ts" in df.columns and "timestamp" not in df.columns:
                    df["timestamp"] = (
                        pd.to_datetime(df["ts"], unit="s")
                        .dt.strftime("%Y-%m-%dT%H:%M:%S%z")
                    )
                dfs.append(df)
            except Exception as e:
                logger.info(f"⚠️ 跳过 {f}: {e}")
                continue

    # 必需列校验：官方 40 列 CSV（无 reward/target/node_delay/node_type）即使混入也会被拦下
    required_cols = {"reward", "target", "node_delay", "node_type", "group_name"}
    validated = []
    for df in dfs:
        missing = required_cols - set(df.columns)
        if missing:
            logger.warning(f"⚠️ 丢弃缺列样本（缺 {sorted(missing)}，疑似官方 smart_weight_data.csv，不用于 PLD 训练）")
            continue
        validated.append(df)
    dfs = validated

    if not dfs:
        raise ValueError("无可用数据（所有文件缺 PLD 必需列）")
    merged = pd.concat(dfs, ignore_index=True)
    logger.info(f"📊 加载 {len(merged)} 条")
    return merged


def build_plf_features(df: pd.DataFrame) -> pd.DataFrame:
    """从 raw CSV 行构造 PLD 30 特征。"""
    out = pd.DataFrame(index=df.index)

    out["asn_feature"] = df["asn_raw"].fillna("").map(extract_asn_feature)
    out["is_udp"] = df["is_udp"].astype(float)

    hs, hc, pk = [], [], []
    for ts in df["timestamp"]:
        s, c, p = hour_features(str(ts))
        hs.append(s); hc.append(c); pk.append(p)
    out["hour_sin"] = hs
    out["hour_cos"] = hc
    out["is_peak"] = pk

    out["domain_feature"] = df["host_raw"].fillna("").map(extract_domain_feature)
    out["geo_feature"] = df["geoip_raw"].fillna("").map(extract_geo_feature)
    out["port_feature"] = df["port_raw"].fillna("0").map(extract_port_feature)
    out["traffic_class"] = [
        classify_traffic(bool(u), p)
        for u, p in zip(df["is_udp"].fillna(0).astype(int), df["port_raw"].fillna("0"))
    ]

    out["asn_hash"] = [fnv1a_hash(str(v), 500) for v in df["asn_raw"]]
    out["host_hash"] = [fnv1a_hash(str(v), 1000) for v in df["host_raw"]]
    out["ip_hash"] = [fnv1a_hash(str(v), 10000) for v in df["ip_raw"]]

    # 站点集群 / 策略组 / 协议类型（新内核 collector 才有 node_delay/node_type 列）
    out["target_hash"] = [fnv1a_hash(str(v), 1000) for v in df["target"]] if "target" in df.columns else 0.0
    out["group_hash"] = [fnv1a_hash(str(v), 200) for v in df["group_name"]]
    if "node_type" in df.columns:
        out["node_type"] = pd.to_numeric(df["node_type"], errors="coerce").fillna(0.0)
    else:
        out["node_type"] = 0.0
    if "node_delay" in df.columns:
        d = pd.to_numeric(df["node_delay"], errors="coerce").fillna(0.0).clip(lower=0)
        out["node_delay"] = np.log1p(d / 100.0)
    else:
        out["node_delay"] = 0.0

    # 节点侧：reward EMA(按 target+节点) + 样本数 + 方差 + 节点哈希
    out["node_reward_ema"] = 0.0
    out["node_sample_count"] = 0.0
    out["node_reward_var"] = 0.0
    out["node_hash"] = [fnv1a_hash(str(v), 1000) for v in df["node_name"]]

    # 与内核 stats 的 (group, config, target, node) 键语义对齐：reward EMA 按
    # (target, node) 分组累计（每个目标站点各自的节点表现）。旧 CSV 无 target
    # 列（内核旧版 collector 输出）时回退为仅按 node 分组。
    if "target" in df.columns:
        group_key = df["target"].fillna("").astype(str) + "\x00" + df["node_name"].astype(str)
    else:
        group_key = df["node_name"].astype(str)
    for _, g in df.groupby(group_key, sort=False):
        rewards = pd.to_numeric(g["reward"], errors="coerce").fillna(0).values
        ema = 0.0
        alpha = 0.4
        emas = []
        for r in rewards:
            ema = r if ema == 0 else (ema * (1 - alpha) + r * alpha)
            emas.append(ema)
        out.loc[g.index, "node_reward_ema"] = np.clip(emas, 0, 1)
        out.loc[g.index, "node_sample_count"] = np.log1p(np.arange(1, len(g) + 1))
        # reward 样本方差（同组窗口），与内核 RewardVar 的"不确定性"语义对齐
        var = float(np.var(rewards)) if len(rewards) > 1 else 0.0
        out.loc[g.index, "node_reward_var"] = min(max(var, 0.0), 2.0) / 2.0

    # 域名流量档位（feature 20）：内置表优先（强语义，视频 CDN 子域），
    # 未命中则按 target 聚合 download_mb 均值计算实测画像，冷启动为 0。
    if "download_mb" in df.columns and "target" in df.columns:
        tg_key = df["target"].fillna("").astype(str)
        tg_avg = df.groupby(tg_key)["download_mb"].mean()
        tg_peak = df.groupby(tg_key)["maxdownloadrate_kb"].max() if "maxdownloadrate_kb" in df.columns else None
        target_level = {}
        for tg, v in tg_avg.items():
            pk = 0.0
            if tg_peak is not None and tg in tg_peak.index:
                pk = float(tg_peak.loc[tg] or 0.0)
            target_level[tg] = traffic_level_from_avg(float(v), pk)
    else:
        target_level = {}
    # 旧版 CSV 无 target 列时退回 host_raw 作为聚合键（与内核 SmartTarget 语义近似）
    if "target" in df.columns:
        level_key = df["target"].fillna("").astype(str)
    else:
        level_key = df["host_raw"].fillna("").astype(str)
    out["domain_traffic_level"] = [
        builtin_traffic_level(h) or target_level.get(str(t), 0)
        for h, t in zip(df["host_raw"].fillna(""), level_key)
    ]

    # 保留字段 0
    for i in range(21, 30):
        out[f"reserved_{i}"] = 0.0

    return out


def preprocess_pld(df: pd.DataFrame):
    logger.info("[步骤2] 构建 PLD 特征 + reward 目标 + sample_weight 样本权重")
    df["reward"] = pd.to_numeric(df["reward"], errors="coerce")
    # 只保留有有效 reward 的样本
    df = df[df["reward"].notna()].copy()
    if len(df) == 0:
        raise ValueError("没有带有效 reward 的样本")

    feats = build_plf_features(df)
    X = feats[PLD_FEATURE_ORDER]
    y = df["reward"]

    # 样本权重：新内核（dev-smart 双通道版）collector 的 sample_weight 列
    # （下载量对数权重，见内核 smart.SampleWeightFromMB），大流量样本天然
    # 占优、网页小流量不靠数量淹没训练。旧数据/旧 CSV 无该列时等权 1.0。
    if "sample_weight" in df.columns:
        w = pd.to_numeric(df["sample_weight"], errors="coerce").fillna(1.0).clip(lower=0.001)
        logger.info(f"⚖️ 使用样本权重：均值 {w.mean():.3f}，95 分位 {w.quantile(0.95):.3f}，最大 {w.max():.3f}")
    else:
        w = pd.Series(1.0, index=df.index)
        logger.info("⚖️ 数据无 sample_weight 列（旧数据），等权 1.0")

    # 剔除特征/目标/权重 NaN
    mask = X.notna().all(axis=1) & y.notna() & w.notna()
    X = X[mask]
    y = y[mask]
    w = w[mask]
    logger.info(f"🧹 有效样本: {len(X)}  特征: {len(PLD_FEATURE_ORDER)}")
    return X, y, w


def train_model(X_train, y_train, w_train, X_test, y_test, w_test):
    logger.info("--> 正在训练 PLD LightGBM prior 模型...")
    logger.info(f"📊 训练集: {len(X_train)} 条样本, {X_train.shape[1]} 个特征")
    logger.info(f"🧪 验证集: {len(X_test)} 条样本")
    logger.info(f"🔄 最大迭代轮数: {LGBM_PARAMS['n_estimators']}")
    logger.info(f"⏹️ 早停轮数: {EARLY_STOPPING_ROUNDS}")
    logger.info(f"📈 学习率: {LGBM_PARAMS['learning_rate']}")

    train_data = lgb.Dataset(X_train, label=y_train, weight=w_train)
    test_data = lgb.Dataset(X_test, label=y_test, weight=w_test, reference=train_data)
    model = lgb.train(
        LGBM_PARAMS,
        train_data,
        valid_sets=[test_data],
        callbacks=[lgb.early_stopping(EARLY_STOPPING_ROUNDS, verbose=False), lgb.log_evaluation(500)],
    )

    logger.info("✅ 训练完成")
    logger.info(f"🏆 最佳迭代轮数: {model.best_iteration}")

    y_pred = model.predict(X_test, num_iteration=model.best_iteration)
    rmse_score = model.best_score.get("valid_0", {}).get("rmse", None)
    mae_score = mean_absolute_error(y_test, y_pred)
    if rmse_score is not None:
        logger.info(f"📈 验证集 RMSE: {rmse_score:.6f}")
    else:
        logger.info("⚠️ 无法获取验证集 RMSE 分数")
    logger.info(f"📉 验证集 MAE: {mae_score:.6f}")

    return model


def save_model_and_config(model, robust_scaler, feature_order, output_path):
    model.save_model(str(output_path), num_iteration=model.best_iteration)

    order_block = "[order]\n" + "".join(f"{i}={name}\n" for i, name in enumerate(feature_order)) + "[/order]\n"

    robust_indices = [feature_order.index(f) for f in ROBUST_SCALER_FEATURES if f in feature_order]

    definitions_block = "[definitions]\n"
    if robust_indices and robust_scaler is not None:
        definitions_block += (
            f"robust_type=RobustScaler\n"
            f"robust_features={','.join(map(str, robust_indices))}\n"
            f"robust_center={','.join(map(str, robust_scaler.center_))}\n"
            f"robust_scale={','.join(map(str, robust_scaler.scale_))}\n\n"
        )
    definitions_block += "[/definitions]\n"

    transformed = set(robust_indices)
    untransformed = [f"{i}:{name}" for i, name in enumerate(feature_order) if i not in transformed]

    final = (
        "\n\nend of trees\n\n"
        f"[transforms]\n{order_block}{definitions_block}"
        f"untransformed_features={','.join(untransformed)}\ntransform=true\n[/transforms]\n"
    )
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(final)
    logger.info("✅ 变换配置已附加到模型末尾")


def setup_logging():
    """train.py 同款日志：纯 message 格式（无 asctime 前缀），console + file 双输出。"""
    log_path = Path(LOG_FILENAME)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    formatter = logging.Formatter("%(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILENAME, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    lgb_logger = logging.getLogger("LightGBM")
    lgb_logger.setLevel(logging.INFO)
    lgb_logger.propagate = True

    try:
        lgb.register_logger(root_logger)
    except AttributeError:
        pass

    logger.info(f"--- 训练开始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def run_training():
    parser = argparse.ArgumentParser(description="Train PLD prior model")
    parser.add_argument("--data_dir", type=Path, default=SCRIPT_DIR / "data")
    parser.add_argument("--output", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()

    setup_logging()
    print_separator("PLD Prior 模型训练开始")

    # 特征 Schema 校验段
    logger.info("[特征 Schema 校验]")
    logger.info(f"📋 PLD 特征定义数量: {len(PLD_FEATURE_ORDER)}")
    logger.info(f"🔍 RobustScaler 配置: {len(ROBUST_SCALER_FEATURES)} 个特征")
    untransformed = [f for f in PLD_FEATURE_ORDER if f not in ROBUST_SCALER_FEATURES]
    if untransformed:
        logger.info(f"⚠️ 以下特征未配置变换策略，将保持原始值: {untransformed[:10]}... (共 {len(untransformed)})")
    logger.info("✓ 特征 Schema 校验通过")

    try:
        df = load_data(args.data_dir)

        # 节点分布健康检查：检测反馈循环/选择偏差
        data_healthy = check_data_balance(df)
        if not data_healthy:
            logger.info("⚠️ 数据分布不健康，本次训练仍在继续，但建议复核数据来源/探索率")

        X, y, w = preprocess_pld(df)

        X_train, X_test, y_train, y_test, w_train, w_test = train_test_split(
            X, y, w, test_size=0.2, random_state=42
        )
        logger.info(f"🧠 训练集: {len(X_train)} 条 | 🧪 验证集: {len(X_test)} 条")

        # RobustScaler on the noisy count feature
        robust_cols = [c for c in ROBUST_SCALER_FEATURES if c in X_train.columns]
        robust_scaler = None
        if robust_cols:
            logger.info("[特征变换]")
            logger.info(f"🔄 RobustScaler 将处理 {len(robust_cols)} 个特征")
            robust_scaler = RobustScaler()
            robust_scaler.fit(X_train[robust_cols])
            logger.info("✓ RobustScaler 已在训练集上拟合")

        model = train_model(X_train, y_train, w_train, X_test, y_test, w_test)
        save_model_and_config(model, robust_scaler, PLD_FEATURE_ORDER, args.output)
        logger.info(f"📦 模型已保存: {args.output}")

        print_separator("训练完成")
        logger.info(f"🎉 最终模型 '{args.output}' 已生成，随时可以部署！")

        header = (
            f"✅ <b>PLD 训练成功</b>\n"
            f"📊 数据量: <code>{len(df)}</code> 条\n"
            f"🔄 训练轮数: <code>{model.best_iteration}</code>\n"
            f"🎯 模型: <code>{args.output}</code>"
        )
        send_telegram_logs(header)
    except Exception:
        logger.error(traceback.format_exc())
        header = (
            f"❌ <b>PLD 训练失败</b>\n"
            f"⚠️ 错误原因: <code>{traceback.format_exc()[-500:]}</code>"
        )
        send_telegram_logs(header)
        raise


if __name__ == "__main__":
    run_training()