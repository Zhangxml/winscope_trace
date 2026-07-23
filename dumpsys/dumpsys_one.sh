#!/bin/bash

# Linux版本的dumpsys收集脚本
# 对应Windows的1-dumpsys_one.bat

# 等待设备连接
adb wait-for-device
adb root

adb wait-for-device

while true; do
    # 获取当前时间戳，格式：YYYYMMDD_HHMMSS
    # Linux使用date命令替代Windows的%date%和%time%
    str_date_time=$(date +"%Y%m%d_%H%M%S")
    
    echo "dumpsys activity"
    adb shell dumpsys activity > "activity_${str_date_time}.txt"
    
    echo "dumpsys window"
    adb shell dumpsys window windows > "window_${str_date_time}.txt"
    
    echo "dumpsys SurfaceFlinger"
    adb shell dumpsys SurfaceFlinger > "SurfaceFlinger_${str_date_time}.txt"
    
    echo "dumpsys display"
    adb shell dumpsys display > "display_${str_date_time}.txt"
    
    echo "dumpsys input"
    adb shell dumpsys input > "input_${str_date_time}.txt"
    
    # 截取屏幕截图
    adb shell screencap -p /data/local/tmp/screen.png
    adb pull /data/local/tmp/screen.png "./${str_date_time}_0_screen.png"
    
    echo "一轮数据收集完成，按Ctrl+C退出，或等待下一轮..."
    echo "按回车键继续下一轮收集..."
    read -p ""
done

exit 0