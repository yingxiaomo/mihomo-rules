#!/usr/bin/env python3
"""Train four priors from Sage native events; PLD is never accepted."""
from __future__ import annotations
import argparse, bisect, hashlib, json, logging, math, os, shutil, sys, tempfile, time, traceback
from pathlib import Path
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

SCHEMA_VERSION=3; SOURCE_SCHEMA="sage-native-events-v2"; FEATURE_SCHEMA="sage-prior-native-features-v2"
MAX_PRIOR_CONFIDENCE=.35; MIN_TRAIN_ROWS=20; MIN_VALID_ROWS=5; MIN_CONFIDENCE_ROWS=20
FEATURE_ORDER=["is_udp","is_tcp","port_log","asn_category","host_category","ip_category","group_category","node_type_category","node_identity_category_a","node_identity_category_b","scene_category"]
CATEGORICAL_FEATURES=FEATURE_ORDER[3:]
MAX_CATEGORICAL_VALUE=2_147_483_647
CONNECT_BOUNDARIES=("dial_return","first_positive_write","first_positive_datagram")
CONNECT_SEMANTICS="successful_availability_elapsed_v1"
LABEL_SEMANTICS={"connect":"successful_availability_duration","response":"first_positive_response_duration","throughput":"active_segment_goodput","failure":"terminal_attempt_failure"}
DIMENSIONS=("connect","response","throughput","failure"); PARTITIONS=("train","early_stop","calibration","final_test")
FAILURE_MIN_CLASS_ATTEMPTS={"train":8,"early_stop":8,"calibration":3,"final_test":3}
MODEL_PARAMS={"verbosity":-1,"learning_rate":.035,"num_leaves":24,"min_data_in_leaf":12,"feature_fraction":.9,"bagging_fraction":.9,"bagging_freq":1,"lambda_l1":.1,"lambda_l2":.5,"seed":20261115,"feature_fraction_seed":20261115,"bagging_seed":20261115,"data_random_seed":20261115,"deterministic":True,"force_col_wise":True,"num_threads":1}
ALGORITHM_CONTRACT={"name":"sage-prior-training-v3","source_schema":SOURCE_SCHEMA,"feature_schema":FEATURE_SCHEMA,"feature_order":FEATURE_ORDER,"categorical_features":CATEGORICAL_FEATURES,"connect_semantics":CONNECT_SEMANTICS,"label_semantics":LABEL_SEMANTICS,"dimensions":DIMENSIONS,"partitions":PARTITIONS,"partition_ratios":[.60,.15,.10,.15],"minimum_partition_attempts":{"train":MIN_TRAIN_ROWS,"early_stop":MIN_VALID_ROWS,"calibration":MIN_VALID_ROWS,"final_test":MIN_VALID_ROWS},"partition_ties":"same decision_unix_nano group is never split; choose nearest feasible group-safe cuts","attempt_context":"exactly one decision event; every event must match decision_unix_nano, transport, features, group_id, node_id, node_id_stable, privacy_key_id and scene; node_id_stable is a strict JSON boolean; observed_unix_nano >= decision_unix_nano","identity_eligibility":"stable non-empty privacy_key_id/node_id and nonzero node type/identity categories; stable identity and identity category pair must form a collision-free bijection within input","connect_label":"event_type=connect, metric=connect, allowed boundary, elapsed_nano/1e6; setup never labels connect","failure_min_class_attempts":FAILURE_MIN_CLASS_ATTEMPTS,"late_label_rule":"observed_unix_nano < next_partition_decision_unix_nano; no refill","throughput_weight":"sum per attempt = 1","baselines":{"regression":"train weighted median","failure":"train weighted failure prevalence"},"failure_calibration":"logistic regression on calibration raw score only when both success and failure meet the partition class minimum","confidence":"min(0.35,max(0,1-model_error/baseline_error)); failure class gate requires both classes in every partition and final_test>=20","model_params":MODEL_PARAMS}
ALGORITHM_SIGNATURE=hashlib.sha256(json.dumps(ALGORITHM_CONTRACT,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()

LOGGER = logging.getLogger("sage_prior_trainer")
DEFAULT_LOG_PATH = Path(__file__).with_name("training_sage.log")

def setup_logging(path=DEFAULT_LOG_PATH):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 _close_logging();LOGGER.setLevel(logging.INFO);LOGGER.propagate=False
 formatter=logging.Formatter("%(message)s")
 file_handler=None;console=None
 try:
  file_handler=logging.FileHandler(path,mode="w",encoding="utf-8");file_handler.setFormatter(formatter)
  console=logging.StreamHandler(sys.stdout);console.setFormatter(formatter)
  LOGGER.addHandler(console);LOGGER.addHandler(file_handler)
 except Exception:
  for handler in (file_handler,console):
   if handler is not None:handler.close()
  LOGGER.handlers.clear();raise
 try:lgb.register_logger(LOGGER)
 except (AttributeError,TypeError):pass
 LOGGER.info(f"--- Sage 训练开始: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
 return path

def _close_logging():
 for handler in list(LOGGER.handlers):
  handler.flush();handler.close();LOGGER.removeHandler(handler)

def _separator(title):
 LOGGER.info("="*72);LOGGER.info(title);LOGGER.info("="*72)

def _format_ns(value):
 if value is None:return "结束"
 return time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime(int(value)/1_000_000_000))

def _canonical(e): return json.dumps(e,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()

def _reject_json_constant(value):
 raise ValueError(f"JSON 常量 {value} 非法")

def _validated_features(value,where="feature"):
 if not isinstance(value,(list,tuple)) or len(value)!=len(FEATURE_ORDER):raise ValueError(f"{where} ABI 无效")
 try:values=np.asarray(value,float)
 except (TypeError,ValueError) as exc:raise ValueError(f"{where} ABI 无效") from exc
 if not np.isfinite(values).all():raise ValueError(f"{where} 必须全部有限")
 for name,raw in zip(CATEGORICAL_FEATURES,values[3:]):
  if not float(raw).is_integer() or raw<0 or raw>MAX_CATEGORICAL_VALUE:raise ValueError(f"{where} categorical {name} 必须是 0..{MAX_CATEGORICAL_VALUE} 的整数")
 return values

def load_native_events(data_dir):
 root=Path(data_dir)
 LOGGER.info("[步骤1] 加载并校验 Sage 原生事件")
 LOGGER.info(f"📂 数据目录: {root.resolve()}")
 compromised=set()
 for marker in [*root.rglob("*.jsonl.failed"),*root.rglob("*.jsonl.recovered")]:
  parts=marker.name.split(".")
  if len(parts)<3 or parts[0]!="sage-native-v2": raise ValueError(f"无法识别损坏采集文件 {marker}")
  compromised.add(parts[1])
 files=sorted(p for p in root.rglob("sage-native-v2.*.jsonl.ready") if p.is_file() and (len(p.name.split("."))<2 or p.name.split(".")[1] not in compromised))
 if not files: raise ValueError("未找到 Sage 原生 .jsonl.ready 数据")
 LOGGER.info(f"📦 可用闭合文件: {len(files)} 个 | 隔离 run: {len(compromised)} 个")
 by_id={};file_summaries=[]
 for path in files:
  header=footer=False; run_id=""; local=[]; footer_event=None
  with path.open(encoding="utf-8") as stream:
   for no,raw in enumerate(stream,1):
    if not raw.endswith("\n"): raise ValueError(f"{path}:{no} 非完整 JSONL")
    try:e=json.loads(raw,parse_constant=_reject_json_constant)
    except (json.JSONDecodeError,ValueError) as exc: raise ValueError(f"{path}:{no} JSON 无效") from exc
    if not isinstance(e,dict) or e.get("source_schema")!=SOURCE_SCHEMA: raise ValueError(f"{path}:{no} source_schema 不兼容")
    kind=e.get("event_type")
    if kind=="file_header":
     if header or footer or e.get("feature_schema")!=FEATURE_SCHEMA or not e.get("run_id"): raise ValueError(f"{path} header 无效")
     header=True; run_id=str(e["run_id"]); continue
    if kind=="file_footer":
     if not header or footer or str(e.get("run_id",""))!=run_id: raise ValueError(f"{path} footer 无效")
     if int(e.get("dropped_control",0)) or int(e.get("dropped_segment",0)) or int(e.get("incomplete_attempts",0)): raise ValueError(f"{path} producer 报告事件丢失")
     footer=True; footer_event=e; continue
    if not header or footer or str(e.get("run_id",""))!=run_id: raise ValueError(f"{path}:{no} 文件边界/run_id 无效")
    feat=e.get("features"); event_id=str(e.get("event_id","")); attempt_id=str(e.get("attempt_id",""))
    if e.get("feature_schema")!=FEATURE_SCHEMA or not event_id or not attempt_id: raise ValueError(f"{path}:{no} 事件 ABI 无效")
    _validated_features(feat,f"{path}:{no} feature")
    local.append(e)
  if not header or not footer or footer_event is None: raise ValueError(f"{path} 缺少 header/footer")
  sequences=[int(e["event_seq"]) for e in local]
  actual_first=min(sequences) if sequences else 0; actual_last=max(sequences) if sequences else 0
  if int(footer_event.get("written_events",-1))!=len(local) or int(footer_event.get("first_event_seq",-1))!=actual_first or int(footer_event.get("last_event_seq",-1))!=actual_last: raise ValueError(f"{path} footer 声明与事件内容不一致")
  file_summaries.append(f"{path.name}: run={run_id} | events={len(local)} | seq={actual_first}..{actual_last}")
  for e in local:
   payload=_canonical(e); event_id=str(e["event_id"]); old=by_id.get(event_id)
   if old and old[0]!=payload: raise ValueError(f"event_id {event_id} 冲突")
   by_id[event_id]=(payload,e)
 frame=pd.DataFrame([x[1] for x in by_id.values()])
 if frame.empty: raise ValueError("没有可训练事件")
 for col in ("event_seq","decision_unix_nano","observed_unix_nano"): frame[col]=pd.to_numeric(frame[col],errors="raise").astype(np.int64)
 for run,group in frame.groupby("run_id",sort=False):
  seq=np.sort(group.event_seq.to_numpy(np.int64))
  if len(seq) and seq[0]!=1: raise ValueError(f"run {run} event_seq 未从 1 开始")
  if len(seq)>1 and np.any(np.diff(seq)!=1): raise ValueError(f"run {run} event_seq 不连续")
 counts=frame.event_type.value_counts().sort_index()
 shown=file_summaries if len(file_summaries)<=6 else [*file_summaries[:3],f"... 省略 {len(file_summaries)-6} 个已通过文件 ...",*file_summaries[-3:]]
 for summary in shown:LOGGER.info(f"  ✓ {summary}")
 LOGGER.info(f"✅ 原生事件校验通过: {len(frame)} 条去重事件 | {frame.run_id.nunique()} 个 run | {frame.attempt_id.nunique()} 个 attempt")
 LOGGER.info("📊 事件类型: "+" | ".join(f"{kind}={int(count)}" for kind,count in counts.items()))
 LOGGER.info(f"🕒 时间范围: {_format_ns(frame.observed_unix_nano.min())} → {_format_ns(frame.observed_unix_nano.max())}")
 return frame.sort_values(["decision_unix_nano","attempt_id","event_seq"],kind="stable").reset_index(drop=True)

def build_features(events):
 rows=[]
 for i,v in events.features.items():
  rows.append(_validated_features(v,f"事件 {i} feature"))
 frame=pd.DataFrame(rows,columns=FEATURE_ORDER,index=events.index)
 frame[CATEGORICAL_FEATURES]=frame[CATEGORICAL_FEATURES].astype(np.int64)
 return frame

def _global_attempts(events):
 return events.groupby("attempt_id",as_index=False).agg(decision_unix_nano=("decision_unix_nano","min")).sort_values(["decision_unix_nano","attempt_id"],kind="stable").reset_index(drop=True)

def _nearest_position(positions,target,lower,upper):
 left=bisect.bisect_left(positions,lower);right=bisect.bisect_right(positions,upper)
 if left>=right:return None
 pivot=bisect.bisect_left(positions,target,left,right);candidates=[]
 if pivot<right:candidates.append(positions[pivot])
 if pivot>left:candidates.append(positions[pivot-1])
 return min(candidates,key=lambda value:(abs(value-target),value))

def _group_safe_cuts(attempts):
 count=len(attempts)
 if count<=0:return [0,0,0,0,count]
 group_ends=attempts.groupby("decision_unix_nano",sort=False).size().cumsum().astype(int).tolist();boundaries=group_ends[:-1];targets=[int(count*.60),int(count*.75),int(count*.85)]
 if count>=MIN_TRAIN_ROWS+3*MIN_VALID_ROWS:
  b2_options=[]
  for b2 in boundaries:
   b3=_nearest_position(boundaries,targets[2],b2+MIN_VALID_ROWS,count-MIN_VALID_ROWS)
   if b3 is not None:b2_options.append((b2,b3,abs(b2-targets[1])+abs(b3-targets[2])))
  suffix=[None]*len(b2_options);best=None
  for index in range(len(b2_options)-1,-1,-1):
   candidate=b2_options[index];best=min((candidate,best) if best is not None else (candidate,),key=lambda item:(item[2],item[0],item[1]));suffix[index]=best
  b2_values=[item[0] for item in b2_options];chosen=None
  for b1 in boundaries:
   if b1<MIN_TRAIN_ROWS:continue
   index=bisect.bisect_left(b2_values,b1+MIN_VALID_ROWS)
   if index>=len(suffix):continue
   b2,b3,cost=suffix[index];candidate=(abs(b1-targets[0])+cost,b1,b2,b3)
   if chosen is None or candidate<chosen:chosen=candidate
  if chosen is not None:return [0,chosen[1],chosen[2],chosen[3],count]
 cuts=[0]
 for target in targets:
  value=_nearest_position(group_ends,target,cuts[-1],count)
  cuts.append(count if value is None else value)
 cuts.append(count);return cuts

def _attempt_partitions(events):
 attempts=_global_attempts(events);cuts=_group_safe_cuts(attempts);attempts["partition"]=""
 boundaries={}
 for name,left,right in zip(PARTITIONS,cuts[:-1],cuts[1:]):
  attempts.loc[left:right-1,"partition"]=name
  start=int(attempts.iloc[left].decision_unix_nano) if left<right else None
  upper=int(attempts.iloc[right].decision_unix_nano) if right<len(attempts) else None
  boundaries[name]={"start_inclusive_decision_unix_nano":start,"end_exclusive_decision_unix_nano":upper,"observed_before_unix_nano":upper}
 return attempts,boundaries

def _validated_attempt_context(events):
 decisions=events.loc[events.event_type.eq("decision")].copy();counts=decisions.groupby("attempt_id").size();all_attempts=set(events.attempt_id.astype(str));missing=sorted(all_attempts-set(counts.index.astype(str)));duplicates=sorted(counts.loc[counts.ne(1)].index.astype(str))
 if missing or duplicates:raise ValueError(f"attempt decision 事件必须恰好一条: missing={missing[:5]} duplicates={duplicates[:5]}")
 canonical=decisions.set_index("attempt_id");work=events.copy()
 for index,value in work.features.items():_validated_features(value,f"事件 {index} feature")
 decision_map=work.attempt_id.map(canonical.decision_unix_nano)
 bad_fields=[]
 for field in ("transport","group_id","node_id","node_id_stable","privacy_key_id","scene"):
  if field not in work.columns:raise ValueError(f"attempt context 缺少 {field}")
  expected=work.attempt_id.map(canonical[field]);bad=work[field].ne(expected)
  if bad.any():bad_fields.append((field,work.loc[bad,"attempt_id"].astype(str).drop_duplicates().tolist()[:5]))
 feature_map=work.attempt_id.map(canonical.features);bad_features=pd.Series([_canonical(left)!=_canonical(right) for left,right in zip(work.features,feature_map)],index=work.index);bad_time=work.decision_unix_nano.ne(decision_map);bad_observed=work.observed_unix_nano.lt(decision_map)
 if bad_time.any():raise ValueError(f"attempt 标签 decision_unix_nano 与 decision 事件不一致: {work.loc[bad_time,'attempt_id'].astype(str).drop_duplicates().tolist()[:5]}")
 if bad_fields:raise ValueError("attempt context 与 decision 事件不一致: "+"; ".join(f"{field}={attempts}" for field,attempts in bad_fields))
 if bad_features.any():raise ValueError(f"attempt 标签 features 与 decision 事件不一致: {work.loc[bad_features,'attempt_id'].astype(str).drop_duplicates().tolist()[:5]}")
 if bad_observed.any():raise ValueError(f"事件 observed_unix_nano 早于 decision: {work.loc[bad_observed,'attempt_id'].astype(str).drop_duplicates().tolist()[:5]}")
 identities_by_pair={}
 pairs_by_identity={}
 stable_values=work["node_id_stable"].map(lambda value:type(value) is bool)
 if not stable_values.all():raise ValueError(f"node_id_stable 必须是 JSON boolean: {work.loc[~stable_values,'attempt_id'].astype(str).drop_duplicates().tolist()[:5]}")
 for _,row in decisions.iterrows():
  privacy=str(row.get("privacy_key_id","") or "").strip();node=str(row.get("node_id","") or "").strip();stable=bool(row.get("node_id_stable",False))
  values=_validated_features(row.features,"decision feature");pair=(int(values[8]),int(values[9]))
  if not stable or not privacy or not node or pair==(0,0):continue
  identity=(privacy,node);previous_identity=identities_by_pair.get(pair);previous_pair=pairs_by_identity.get(identity)
  if previous_identity is not None and previous_identity!=identity:raise ValueError(f"node identity category pair 碰撞: pair={pair} identities={previous_identity},{identity}")
  if previous_pair is not None and previous_pair!=pair:raise ValueError(f"node identity category pair 不稳定: identity={identity} pairs={previous_pair},{pair}")
  identities_by_pair[pair]=identity;pairs_by_identity[identity]=pair
 work["decision_unix_nano"]=decision_map.astype(np.int64);work["features"]=feature_map
 for field in ("transport","group_id","node_id","node_id_stable","privacy_key_id","scene"):work[field]=work.attempt_id.map(canonical[field])
 return work

def _failure_class_gate(table):
 counts={}
 deficits=[]
 for part in PARTITIONS:
  frame=table.loc[table.partition.eq(part),["attempt_id","label"]].drop_duplicates()
  success=int(frame.loc[frame.label.eq(0),"attempt_id"].nunique());failure=int(frame.loc[frame.label.eq(1),"attempt_id"].nunique());required=FAILURE_MIN_CLASS_ATTEMPTS[part]
  counts[part]={"success":success,"failure":failure}
  for class_name,actual in (("success",success),("failure",failure)):
   if actual<required:deficits.append({"partition":part,"class":class_name,"actual":actual,"required":required,"missing":required-actual})
 return {"passed":not deficits,"minimums":dict(FAILURE_MIN_CLASS_ATTEMPTS),"counts":counts,"deficits":deficits}

def _dimension_preflight(name,table):
 counts={part:int(table.loc[table.partition.eq(part),"attempt_id"].nunique()) for part in PARTITIONS}
 result={"partition_attempts":counts,"partition_boundaries":table.attrs.get("partition_boundaries",{}),"candidate_rows":int(table.attrs.get("candidate_rows",len(table))),"invalid_rows":int(table.attrs.get("invalid_rows",0)),"late_dropped_rows":int(table.attrs.get("late_dropped_rows",0)),"retained_rows":int(len(table))}
 if name=="failure":result["class_gate"]=_failure_class_gate(table)
 return result

def preflight_training_tables(tables):
 _separator("[步骤3] 四维训练前统一预检")
 report={};blocking=[]
 for name in DIMENSIONS:
  if name not in tables:
   blocking.append(f"{name}: 缺少训练表");continue
  evidence=_dimension_preflight(name,tables[name]);report[name]=evidence;counts=evidence["partition_attempts"]
  LOGGER.info(f"🔎 {name:<10} "+" | ".join(f"{part}={counts[part]}" for part in PARTITIONS))
  missing=[]
  if counts["train"]<MIN_TRAIN_ROWS:missing.append(f"train 缺 {MIN_TRAIN_ROWS-counts['train']}")
  for part in PARTITIONS[1:]:
   if counts[part]<MIN_VALID_ROWS:missing.append(f"{part} 缺 {MIN_VALID_ROWS-counts[part]}")
  if missing:blocking.append(f"{name}: "+", ".join(missing))
  if name=="failure":
   gate=evidence["class_gate"]
   LOGGER.info("🧪 failure 类别: "+" | ".join(f"{part}=success:{gate['counts'][part]['success']},failure:{gate['counts'][part]['failure']},required_each:{gate['minimums'][part]}" for part in PARTITIONS))
   if gate["deficits"]:LOGGER.warning("⚠️ failure 类别门禁不足，confidence 将强制为 0: "+json.dumps(gate["deficits"],ensure_ascii=False,separators=(",",":")))
 if blocking:raise ValueError("四维训练预检失败: "+"; ".join(blocking))
 LOGGER.info("✅ 四维总量预检通过，开始训练")
 return report

def build_training_tables(events):
 LOGGER.info("[步骤2] 按各维有效标签 attempt 独立时间切分并构造四维标签")
 required={"attempt_id","event_type","decision_unix_nano","observed_unix_nano","transport","features","group_id","node_id","node_id_stable","privacy_key_id","scene"}
 if required-set(events.columns): raise ValueError(f"缺少字段 {sorted(required-set(events.columns))}")
 work=_validated_attempt_context(events);attempts=_global_attempts(work);features=build_features(work)
 LOGGER.info(f"🧩 全局独立 attempt: {len(attempts)}")
 LOGGER.info("🧹 各维边界依据=有效标签 attempt 的 decision_unix_nano；迟到过滤严格要求 observed_unix_nano < 下一分区边界；过滤后不补位")
 for name in FEATURE_ORDER: work[name]=features[name]
 empty=pd.Series(index=work.index,dtype=object)
 eligible_identity=work.node_id_stable.eq(True)&work.node_id.fillna("").astype(str).str.strip().ne("")&work.privacy_key_id.fillna("").astype(str).str.strip().ne("")&work.node_type_category.ne(0)&work.node_identity_category_a.ne(0)&work.node_identity_category_b.ne(0)
 specs={"connect":(work.event_type.eq("connect")&work.get("metric",empty).eq("connect")&work.get("boundary",empty).isin(CONNECT_BOUNDARIES),"log_milliseconds"),"response":(work.event_type.eq("response")&work.transport.eq("tcp"),"log_milliseconds"),"throughput":(work.event_type.eq("throughput")&work.transport.eq("tcp"),"log_bits_per_second"),"failure":(work.event_type.eq("outcome")&work.get("outcome",empty).isin(["success","failure"]),"probability")}
 tables={}
 for name,(mask,unit) in specs.items():
  table=work.loc[mask].copy();candidates=len(table);value=pd.to_numeric(table["value"],errors="coerce") if "value" in table else pd.Series(np.nan,index=table.index)
  if name=="connect":
   elapsed=pd.to_numeric(table["elapsed_nano"],errors="coerce") if "elapsed_nano" in table else pd.Series(np.nan,index=table.index);milliseconds=elapsed/1_000_000.;valid=milliseconds.ge(0)&milliseconds.le(30000);table["label"]=np.log(milliseconds.clip(1,30000))
  elif name=="response":valid=value.gt(0)&value.le(30000);table["label"]=np.log(value.clip(1,30000))
  elif name=="throughput":
   byte_count=pd.to_numeric(table["bytes"],errors="coerce") if "bytes" in table else pd.Series(np.nan,index=table.index);elapsed=pd.to_numeric(table["elapsed_nano"],errors="coerce") if "elapsed_nano" in table else pd.Series(np.nan,index=table.index);valid=value.ge(8192)&value.le(1e9)&byte_count.ge(65536)&elapsed.ge(2e9);table["label"]=np.log(value.clip(8192,1e9))
  else: valid=table.get("outcome",empty).isin(["success","failure"]); table["label"]=table.get("outcome",empty).map({"success":0.,"failure":1.})
  valid &= eligible_identity.loc[table.index]
  table=table.loc[valid&table[FEATURE_ORDER].notna().all(axis=1)].copy();invalid_dropped=candidates-len(table)
  if name=="failure":
   conflicts=table.groupby("attempt_id").label.nunique();conflicts=conflicts.loc[conflicts.gt(1)]
   if len(conflicts):raise ValueError(f"failure attempt 终态冲突: {conflicts.index.tolist()[:5]}")
  label_attempts,boundaries=_attempt_partitions(table);table["partition"]=table.attempt_id.map(label_attempts.set_index("attempt_id").partition)
  before_late=len(table);keep=pd.Series(True,index=table.index)
  for part,boundary in boundaries.items():
   upper=boundary["observed_before_unix_nano"]
   if upper is not None:keep &= ~table.partition.eq(part)|table.observed_unix_nano.lt(upper)
  table=table.loc[keep].copy();late_dropped=before_late-len(table);table["weight"]=1.
  if name=="throughput":table["weight"]=1./table.groupby("attempt_id").attempt_id.transform("count").clip(lower=1)
  table["unit"]=unit;table=table.sort_values(["decision_unix_nano","attempt_id","event_seq"],kind="stable").reset_index(drop=True)
  by_partition={part:int(table.loc[table.partition.eq(part),"attempt_id"].nunique()) for part in PARTITIONS}
  table.attrs={"partition_boundaries":boundaries,"partition_attempts":by_partition,"candidate_rows":candidates,"invalid_rows":invalid_dropped,"late_dropped_rows":late_dropped}
  tables[name]=table
  LOGGER.info(f"  • {name:<10} candidate={candidates} → invalid/range dropped={invalid_dropped} → late dropped={late_dropped} → retained={len(table)} | 独立 attempt={table.attempt_id.nunique()} | "+" | ".join(f"{part}={by_partition[part]}" for part in PARTITIONS))
  LOGGER.info("    分区上界: "+" | ".join(f"{part}<{_format_ns(boundaries[part]['end_exclusive_decision_unix_nano'])}" for part in PARTITIONS))
  if name=="throughput":LOGGER.info("    throughput 权重: 同一 attempt 内各窗口权重和固定为 1")
 return attempts,tables

def _confidence(a,b):
 if not(np.isfinite(a) and np.isfinite(b)) or b<=1e-12:return 0.
 return round(min(MAX_PRIOR_CONFIDENCE,max(0.,1-a/b)),6)

def _weighted_median(v,w):
 v,w=np.asarray(v,float),np.asarray(w,float); valid=np.isfinite(v)&np.isfinite(w)&(w>0)
 if not np.any(valid):raise ValueError("加权中位数需要正权重")
 v,w=v[valid],w[valid]; order=np.argsort(v,kind="stable");v,w=v[order],w[order]
 return float(v[min(int(np.searchsorted(np.cumsum(w),w.sum()*.5,side="left")),len(v)-1)])

def train_dimension(name,table,preflight=None):
 _separator(f"[步骤4] 训练 {name} 维度")
 evidence=preflight or _dimension_preflight(name,table);parts={p:table.loc[table.partition.eq(p)].reset_index(drop=True) for p in PARTITIONS};counts=evidence["partition_attempts"]
 if counts["train"]<MIN_TRAIN_ROWS or any(counts[p]<MIN_VALID_ROWS for p in PARTITIONS[1:]):raise ValueError(f"{name} 独立 attempt 分区不足: {counts}")
 objective,metric=("binary","binary_logloss") if name=="failure" else ("regression_l2","l2");params={**MODEL_PARAMS,"objective":objective,"metric":metric};train,early=parts["train"],parts["early_stop"]
 LOGGER.info("📊 独立 attempt: "+" | ".join(f"{part}={counts[part]}" for part in PARTITIONS))
 LOGGER.info(f"🏷️ 标签: rows={len(table)} | min={table.label.min():.6f} | p50={table.label.median():.6f} | p95={table.label.quantile(.95):.6f} | max={table.label.max():.6f}")
 if name=="failure":LOGGER.info(f"⚖️ 失败标签: success={int((table.label==0).sum())} | failure={int((table.label==1).sum())}")
 LOGGER.info(f"🌲 LightGBM: objective={objective} | metric={metric} | max_rounds=600 | early_stopping=40 | learning_rate={params['learning_rate']} | features={len(FEATURE_ORDER)}")
 ds=lgb.Dataset(train[FEATURE_ORDER],label=train.label,weight=train.weight,feature_name=FEATURE_ORDER,categorical_feature=CATEGORICAL_FEATURES);es=lgb.Dataset(early[FEATURE_ORDER],label=early.label,weight=early.weight,reference=ds,feature_name=FEATURE_ORDER,categorical_feature=CATEGORICAL_FEATURES)
 model=lgb.train(params,ds,num_boost_round=600,valid_sets=[es],valid_names=["early_stop"],callbacks=[lgb.early_stopping(40,verbose=True),lgb.log_evaluation(50)])
 LOGGER.info(f"🏆 {name} 最佳迭代: {model.best_iteration}")
 final=parts["final_test"];truth,weights=final.label.to_numpy(float),final.weight.to_numpy(float);ty,tw=train.label.to_numpy(float),train.weight.to_numpy(float);transform,scale,bias="identity",1.,0.;class_gate=None
 if name=="failure":
  transform="sigmoid";class_gate=evidence.get("class_gate") or _failure_class_gate(table);cal=parts["calibration"];cy,cw=cal.label.to_numpy(float),cal.weight.to_numpy(float);raw=np.asarray(model.predict(cal[FEATURE_ORDER],num_iteration=model.best_iteration,raw_score=True),float);calibration_ready=len(np.unique(cy))==2 and class_gate["counts"]["calibration"]["failure"]>=FAILURE_MIN_CLASS_ATTEMPTS["calibration"]
  if calibration_ready:
   lr=LogisticRegression(C=1e6,solver="lbfgs",random_state=20261115).fit(raw.reshape(-1,1),cy,sample_weight=cw);candidate_scale,candidate_bias=float(lr.coef_[0,0]),float(lr.intercept_[0]);calibrated=candidate_scale>0
   if calibrated:scale,bias=candidate_scale,candidate_bias
  else:calibrated=False
  raw=np.asarray(model.predict(final[FEATURE_ORDER],num_iteration=model.best_iteration,raw_score=True),float);pred=1/(1+np.exp(-np.clip(scale*raw+bias,-40,40)));base=float(np.average(ty,weights=tw));error=float(np.average((truth-pred)**2,weights=weights));base_error=float(np.average((truth-base)**2,weights=weights));metric_name="brier";baseline_strategy="train weighted failure prevalence"
 else:
  calibrated=True;pred=np.asarray(model.predict(final[FEATURE_ORDER],num_iteration=model.best_iteration),float);base=_weighted_median(ty,tw);error=float(np.average(abs(truth-pred),weights=weights));base_error=float(np.average(abs(truth-base),weights=weights));metric_name="mae_log";baseline_strategy="train weighted median"
 class_gate_passed=name!="failure" or bool(class_gate["passed"]);confidence=_confidence(error,base_error) if calibrated and class_gate_passed and counts["final_test"]>=MIN_CONFIDENCE_ROWS else 0.
 improvement=(1-error/base_error) if np.isfinite(base_error) and base_error>1e-12 else float("nan")
 LOGGER.info(f"📏 朴素基线: strategy={baseline_strategy} | prediction={base:.9f}")
 LOGGER.info(f"🧪 最终验证: metric={metric_name} | model_error={error:.9f} | baseline_error={base_error:.9f} | improvement={improvement:.2%}")
 if name=="failure" and not class_gate_passed:reason="failure 类别门禁不足: "+", ".join(f"{d['partition']} {d['class']} {d['actual']}/{d['required']} (缺 {d['missing']})" for d in class_gate["deficits"])
 elif not calibrated:reason="校准集类别不足或校准斜率无效"
 elif counts["final_test"]<MIN_CONFIDENCE_ROWS:reason=f"final_test 独立 attempt {counts['final_test']} < {MIN_CONFIDENCE_ROWS}"
 elif not np.isfinite(error):reason="model_error 非有限"
 elif not np.isfinite(base_error) or base_error<=1e-12:reason="baseline_error 非法或无区分度"
 elif improvement<=0:reason="模型未优于朴素基线"
 else:reason="通过"
 LOGGER.info(f"🛡️ confidence={confidence:.6f} / cap={MAX_PRIOR_CONFIDENCE:.2f} | gate={reason}")
 if name=="failure":LOGGER.info(f"🎛️ failure calibration: calibrated={calibrated} | class_gate={class_gate_passed} | scale={scale:.12f} | bias={bias:.12f}")
 training_evidence={**evidence,"confidence_gate":reason,"confidence":confidence,"validation_metric":metric_name,"validation_error":round(error,9),"baseline_error":round(base_error,9)}
 return model,{"unit":str(table.unit.iloc[0]),"label_semantics":LABEL_SEMANTICS[name],"confidence":confidence,"train_samples":counts["train"],"early_stop_samples":counts["early_stop"],"calibration_samples":counts["calibration"],"validation_samples":counts["final_test"],"validation_metric":metric_name,"validation_error":round(error,9),"baseline_error":round(base_error,9),"output_transform":transform,"calibration_scale":round(scale,12),"calibration_bias":round(bias,12),"_evidence":training_evidence}

def _sha256(path):
 h=hashlib.sha256()
 with Path(path).open("rb") as f:
  for block in iter(lambda:f.read(1<<20),b""):h.update(block)
 return h.hexdigest()

def save_bundle(output,attempts,models,metadata):
 _separator("[步骤5] 生成原子化四维模型包")
 output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True);stem=output.stem;dimensions={};staging=Path(tempfile.mkdtemp(prefix=f".{stem}-",dir=output.parent));created=[]
 try:
  for name in DIMENSIONS:
   staged=staging/f"{stem}-{name}.lgb";models[name].save_model(str(staged),num_iteration=models[name].best_iteration);digest=_sha256(staged);target=output.with_name(f"{stem}-{name}-{digest[:16]}.lgb")
   if target.exists() and _sha256(target)!=digest:raise ValueError("模型冲突")
   if not target.exists():os.replace(staged,target);created.append(target)
   runtime_metadata={key:value for key,value in metadata[name].items() if not key.startswith("_")}
   dimensions[name]={**runtime_metadata,"model":target.name,"sha256":digest}
   LOGGER.info(f"  ✓ {name:<10} {target.name} | {target.stat().st_size} bytes | sha256={digest}")
  manifest={"schema_version":SCHEMA_VERSION,"feature_schema":FEATURE_SCHEMA,"source_schema":SOURCE_SCHEMA,"feature_order":FEATURE_ORDER,"categorical_features":CATEGORICAL_FEATURES,"connect_semantics":CONNECT_SEMANTICS,"generated_from_event_unix":int(attempts.decision_unix_nano.max()//1_000_000_000),"max_prior_confidence":MAX_PRIOR_CONFIDENCE,"dimensions":dimensions}
  temp=output.with_suffix(output.suffix+".tmp");temp.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8");os.replace(temp,output)
  LOGGER.info(f"📦 manifest: {output} | {output.stat().st_size} bytes | sha256={_sha256(output)} | generated_from={manifest['generated_from_event_unix']}")
  LOGGER.info("📈 四维 confidence: "+" | ".join(f"{name}={dimensions[name]['confidence']:.6f}" for name in DIMENSIONS))
  return manifest
 except Exception:
  for p in created:p.unlink(missing_ok=True)
  raise
 finally:shutil.rmtree(staging,ignore_errors=True)

def save_training_evidence(output,attempts,metadata,provenance=None):
 output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
 dimensions={name:metadata[name]["_evidence"] for name in DIMENSIONS}
 manifest={"evidence_schema":"sage-prior-training-evidence-v2","algorithm_signature":ALGORITHM_SIGNATURE,"algorithm_contract":ALGORITHM_CONTRACT,"source_schema":SOURCE_SCHEMA,"feature_schema":FEATURE_SCHEMA,"generated_from_event_unix":int(attempts.decision_unix_nano.max()//1_000_000_000),"provenance":provenance or {"training_commit":None,"consumer_commit":None},"dimensions":dimensions}
 temp=output.with_suffix(output.suffix+".tmp");temp.write_text(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8");os.replace(temp,output)
 LOGGER.info(f"📋 training evidence: {output} | {output.stat().st_size} bytes | sha256={_sha256(output)} | algorithm={ALGORITHM_SIGNATURE}")
 return manifest

def save_prediction_parity(output,tables,models,metadata):
 output=Path(output).resolve();output.parent.mkdir(parents=True,exist_ok=True)
 candidates=tables["connect"].loc[tables["connect"].partition.eq("final_test")]
 if candidates.empty:raise ValueError("prediction parity 缺少 final_test feature vector")
 features=[float(candidates.iloc[0][name]) for name in FEATURE_ORDER];sample=pd.DataFrame([features],columns=FEATURE_ORDER);predictions={}
 for name in DIMENSIONS:
  raw=float(np.asarray(models[name].predict(sample,num_iteration=models[name].best_iteration,raw_score=name=="failure"),float)[0]);value=float(metadata[name]["calibration_scale"])*raw+float(metadata[name]["calibration_bias"])
  if metadata[name]["output_transform"]=="sigmoid":value=1/(1+math.exp(-max(-40.,min(40.,value))))
  predictions[name]=value
 payload={"schema":"sage-prior-prediction-parity-v1","feature_order":FEATURE_ORDER,"features":features,"predictions":predictions}
 temp=output.with_suffix(output.suffix+".tmp");temp.write_text(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+"\n",encoding="utf-8");os.replace(temp,output)
 LOGGER.info(f"🔁 prediction parity: {output} | sha256={_sha256(output)}")
 return payload

def _validated_commit(value,name):
 value=str(value or "").strip()
 if value and (len(value)!=40 or any(ch not in "0123456789abcdefABCDEF" for ch in value)):raise ValueError(f"{name} 必须是 40 位 commit SHA")
 return value.lower() or None

def run_training(argv=None):
 p=argparse.ArgumentParser();p.add_argument("--data-dir",type=Path,default=Path(__file__).parent/"sage-data-v2");p.add_argument("--output",type=Path,default=Path(__file__).parent/"SagePrior.json");p.add_argument("--evidence-output",type=Path);p.add_argument("--parity-output",type=Path);p.add_argument("--log-file",type=Path,default=DEFAULT_LOG_PATH);p.add_argument("--training-commit",default=os.environ.get("GITHUB_SHA",""));p.add_argument("--consumer-commit",default=os.environ.get("SAGE_CONSUMER_SHA",""));a=p.parse_args(argv)
 log_path=setup_logging(a.log_file);started=time.monotonic();_separator("Sage 原生四维先验训练")
 LOGGER.info(f"📋 source_schema={SOURCE_SCHEMA} | feature_schema={FEATURE_SCHEMA} | features={len(FEATURE_ORDER)} | algorithm={ALGORITHM_SIGNATURE}")
 try:
  provenance={"training_commit":_validated_commit(a.training_commit,"training_commit"),"consumer_commit":_validated_commit(a.consumer_commit,"consumer_commit")}
  events=load_native_events(a.data_dir);attempts,tables=build_training_tables(events);preflight=preflight_training_tables(tables);models={};metadata={}
  for name in DIMENSIONS:models[name],metadata[name]=train_dimension(name,tables[name],preflight[name])
  manifest=save_bundle(a.output,attempts,models,metadata);evidence_output=a.evidence_output or Path(a.output).with_name(f"{Path(a.output).stem}.evidence.json");save_training_evidence(evidence_output,attempts,metadata,provenance)
  if a.parity_output:save_prediction_parity(a.parity_output,tables,models,metadata)
  elapsed=time.monotonic()-started
  _separator("训练完成")
  LOGGER.info(f"🎉 Sage 四维先验已生成: {Path(a.output).resolve()} | elapsed={elapsed:.2f}s")
  return manifest
 except Exception:
  LOGGER.error(traceback.format_exc())
  raise
 finally:_close_logging()
if __name__=="__main__":run_training()
