#!/bin/bash
# Mihomo CSV 数据上传脚本
# 使用方法: ./upload.sh <本地CSV文件或文件夹路径>

# 配置（修改这里的 remote_name 为你 rclone 配置的名称）
remote_name="gdrive"
remote_path="mihomo-data"

# 检查参数
if [ -z "$1" ]; then
    echo "用法: ./upload.sh <本地CSV文件或文件夹路径>"
    echo "示例: ./upload.sh ./data"
    echo "      ./upload.sh ./mihomo_data.csv"
    exit 1
fi

source_path="$1"

# 检查 rclone 是否安装
if ! command -v rclone &> /dev/null; then
    echo "错误: rclone 未安装"
    echo "安装方法: curl https://rclone.org/install.sh | sudo bash"
    exit 1
fi

# 创建远程文件夹（如果不存在）
echo "创建远程文件夹..."
rclone mkdir "${remote_name}:${remote_path}"

# 上传文件
echo "上传 ${source_path} 到 ${remote_name}:${remote_path} ..."
rclone copy "${source_path}" "${remote_name}:${remote_path}" -P

echo "上传完成！"
