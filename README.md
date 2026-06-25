# LED亮度检测上位机

Windows 上位机程序，用于通过串口轮询 16 个通道的 LED 光照强度数据，并在界面中显示每个通道的 16 路实时数值、历史曲线和原始串口返回。显示和导出单位为 `lux`。

运行链路和异常值排查台账见 [docs/运行逻辑与异常排查.md](docs/运行逻辑与异常排查.md)。

## 功能概览

- 串口通信：`115200, 8N1`
- 通道范围：`1-16`
- 每个通道显示 16 路数据，界面为 4x4 卡片
- 后台持续轮询全部通道，切换通道时显示最近一次成功采集值
- 点击任一路 LED 卡片后，右侧显示该路历史曲线
- 支持显示当前通道最近一次串口 RX 原始 HEX，便于排查硬件和解析问题
- 支持普通保存：导出最近一段历史数据到 Excel
- 支持连续记录：长时间写入临时 CSV，停止后生成 Excel
- 支持设置串口超时、通道间隔、曲线显示、保存间隔、历史点数等参数
- 启动时检查 `requirements.txt` 依赖，缺失时提示并支持自动安装

## 环境要求

- Windows
- Python 3.11 或兼容版本
- 串口设备可被 Windows 识别为 `COMx`

依赖包见 [requirements.txt](requirements.txt)：

```text
PySide6
pyserial
pyqtgraph
openpyxl
pytest
```

## 安装依赖

建议在项目目录中使用虚拟环境：

```powershell
cd "D:\Visual Studio Code Projects\Python\上位机程序"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

如果依赖缺失，程序启动时也会弹窗提示。点击“自动安装”后会显示 pip 安装输出，安装完成后需要重新启动程序。

## 启动程序

```powershell
cd "D:\Visual Studio Code Projects\Python\上位机程序"
python app.py
```

如果只想预览依赖检查弹窗：

```powershell
python app.py --show-dependency-check-demo
```

## 串口使用

1. 插入硬件设备。
2. 启动程序。
3. 点击左上角 `选择COM口`。
4. 选择设备串口，例如 `COM5`。
5. 点击 `连接`。
6. 程序会按 `1-16` 通道循环读取。

读取失败或解析失败时，程序会在状态栏显示错误；该通道界面会继续保留上一次成功采集值，不会自动清零。

## 串口协议

读取命令：

```text
A5 5A XX 02 02 00 00 SUM
```

返回帧：

```text
5A A5 XX 02 40 + 16 个大端 IEEE754 float 光照强度值 + SUM
```

字段说明：

- `XX`：通道号，范围 `0x01` 到 `0x10`
- `02`：功能码
- `40`：payload 长度，十进制 64 字节
- `SUM`：从帧头开始逐字节累加后取低 8 位
- payload：16 个大端 IEEE754 float，单位按 `lux` 显示和导出

当前解析会校验帧头、通道号、功能码、长度和 SUM。只有校验通过的数据才会进入界面、历史曲线和保存链路。

## 数据保存

### 普通保存

关闭连续记录模式时，点击 `保存` 会导出最近一段历史数据。默认路径：

```text
data\YYYY_MM_DD\通道1\光照强度1.xlsx
data\YYYY_MM_DD\通道1\光照强度2.xlsx
...
data\YYYY_MM_DD\通道16\光照强度16.xlsx
```

Excel 第一列为真实采样时间，格式为 `HH:MM:SS.mmm`；第二列为 `光照强度(lux)` 文本。

### 连续记录

连续记录模式在 `设置` -> `高级设置` 中开启。开启后：

- `保存` 按钮用于开始记录
- 记录过程中按钮显示为 `停止`
- 点击 `停止` 后生成 Excel 文件
- 保存时长设置不生效，程序会从开始记录一直保存到手动停止
- 临时 CSV 保存在 `_temp_csv` 目录中，停止后转换为 Excel

保存目录按开始记录时间生成：

```text
data\YYYY_MM_DD\record_HHMMSS\通道1\光照强度1.xlsx
data\YYYY_MM_DD\record_HHMMSS\通道1\光照强度2.xlsx
...
data\YYYY_MM_DD\record_HHMMSS\通道16\光照强度16.xlsx
```

如果同一秒内重新开始连续记录，会复用同名 `record_HHMMSS` 目录并覆盖上一轮记录。

## 设置说明

- `READ_TIMEOUT_SECONDS`：串口读写最大等待时间，过小容易超时，过大会让异常响应变慢
- `ROUND_DELAY_SECONDS`：完成 16 通道一轮读取后的暂停时间
- `CHANNEL_DELAY_SECONDS`：每个通道读取完成后的等待时间
- `CURVE_VISIBLE_SECONDS`：曲线可见时间窗口
- `DEFAULT_MAX_POINTS`：每路历史缓存最大点数
- `DISPLAY_DECIMALS`：卡片显示小数位数
- `SHOW_SERIAL_ERRORS`：是否在状态栏显示串口读取失败
- `SHOW_RAW_SERIAL_VIEW`：是否在右侧显示原始串口返回
- `CONTINUOUS_RECORDING`：是否启用连续记录模式

串口超时和轮询间隔在下一次连接串口时生效；曲线和显示相关设置保存后会立即应用。

## 打包程序

```powershell
pip install pyinstaller
pyinstaller --noconfirm "LED亮度检测上位机.spec"
```

打包完成后，把整个目录发送给同事：

```text
dist\LED亮度检测上位机
```

不要只发送单独的 `.exe`。打包版会使用 `_internal` 中的依赖文件，不再执行开发环境的 `requirements.txt` 依赖检查。

## 测试

```powershell
python -m pytest tests
```

当前测试覆盖：

- 协议请求帧生成
- SUM 校验
- 返回帧解析
- 通道范围检查
- 串口帧头查找和超时处理
- 历史曲线数据缓存
- 设置读取、归一化和保存间隔计算
- 依赖检查解析逻辑
- Excel 导出
- 连续记录临时 CSV 与 Excel 生成

## Git 使用

当前仓库 remote 使用 SSH：

```text
git@github.com:BzhChara/led_brightness_tester.git
```

常用提交流程：

```powershell
git status
git add README.md led_debugger tests app.py requirements.txt settings.json
git commit -m "说明"
git push
```

不要提交以下打包或缓存目录：

```text
build\
dist\
__pycache__\
```

## 当前注意事项

- 显示、曲线和 Excel 导出统一使用 `光照强度(lux)`。
- 如果某通道读取失败，界面会保留该通道上一次成功采集值，不能直接理解为当前硬件实时值。
- `SUM` 校验较弱，不等同 CRC。遇到偶发异常值时，建议开启 `显示串口原始返回`，保留当次原始帧再判断是硬件返回、串口链路还是解析问题。
- 当前没有异常值过滤、跳变过滤或无光基线判断，所有通过协议校验的 float 都会进入显示和保存链路。
