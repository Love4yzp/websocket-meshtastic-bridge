#!/bin/bash

show_usage() {
    echo "用法:"
    echo "  $0 start   # 添加每小时重启的定时任务"
    echo "  $0 stop    # 移除重启定时任务"
    echo "  $0 status  # 查看当前是否有重启定时任务"
}

# 检查参数
if [ $# -ne 1 ]; then
    show_usage
    exit 1
fi

case "$1" in
    "start")
        # 添加定时任务
        (crontab -l 2>/dev/null; echo "0 * * * * docker restart ws-meshtastic") | crontab -
        echo "已添加每小时重启任务"
        ;;
    "stop")
        # 删除定时任务
        crontab -l 2>/dev/null | grep -v "docker restart ws-meshtastic" | crontab -
        echo "已移除重启任务"
        ;;
    "status")
        # 检查是否存在定时任务
        if crontab -l 2>/dev/null | grep -q "docker restart ws-meshtastic"; then
            echo "重启任务正在运行中"
        else
            echo "当前没有重启任务"
        fi
        ;;
    *)
        show_usage
        exit 1
        ;;
esac
