@echo off
:: Mihomo CSV 数据上传脚本 (Windows)
:: 使用方法: upload.bat "C:\path\to\csv文件"

:: 配置（修改这里的 remote_name 为你 rclone 配置的名称）
set remote_name=gdrive
set remote_path=mihomo-data

:: 检查参数
if "%~1"=="" (
    echo 用法: upload.bat "C:\path\to\csv文件或文件夹"
    echo 示例: upload.bat ".\data"
    echo        upload.bat "C:\Users\你\mihomo_data.csv"
    exit /b 1
)

set source_path=%~1

:: 检查 rclone 是否安装
where rclone >nul 2>nul
if errorlevel 1 (
    echo 错误: rclone 未安装
    echo 下载安装: https://rclone.org/downloads/
    exit /b 1
)

:: 创建远程文件夹（如果不存在）
echo 创建远程文件夹...
rclone mkdir "%remote_name%:%remote_path%"

:: 上传文件
echo 上传 %source_path% 到 %remote_name%:%remote_path% ...
rclone copy "%source_path%" "%remote_name%:%remote_path%" -P

echo 上传完成！
