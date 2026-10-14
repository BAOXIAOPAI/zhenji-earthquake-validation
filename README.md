# 震迹 · 地震候选事件智能核查

**以观测和专业计算核查分析条件，改变条件后继续复核。**

![震迹工作台](evidence/screenshots/01-workbench-conclusion-waveform.png)

震迹根据问题、UTC 时窗、台站与目标频带，检查地震波形是否满足方法输入条件，并分析自动到时证据。工作台展示结论、三分量波形、核查过程与来源；更改条件后，可比较前后两次核查。

Agnes Harness 组织模型与专业工具执行，DeepSeek 模型选择分析步骤，ObsPy 等组件完成数值计算，验证器核对结论与证据。

## 安装与启动

使用 Python 3.12，在终端执行：

```sh
git clone https://github.com/BAOXIAOPAI/zhenji-earthquake-validation.git
cd zhenji-earthquake-validation
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
RUNLOG_DIR="$PWD/replay/runlog" EQ_STRICT_OFFLINE=1 .venv/bin/python main/server.py 8791
```

打开 [本地工作台](http://127.0.0.1:8791/)。默认显示已保存的真实核查记录，查看示例不调用模型，也不获取新观测。完整配置与操作见[运行指南](docs/运行指南.md)。

## 工作台

- **核查工作台**：查看问题与条件、结论、波形、核查过程及证据详情。
- **核查记录**：查看每次核查及其条件反馈，成功、未知与失败分别记录。
- **使用说明**：了解方法、数据来源与结果含义，查看首轮和条件复核对比。

波形来自当前记录保存的观测文件。用户调整台站、分析目标或到时检查频带后，系统创建关联上一条记录的新核查，默认复用已有观测，重新计算受影响的分析项。

## 真实示例

随附记录使用 UW.CHIMA 台站的三分量观测，时窗为 2023-12-24 10:00:00–10:02:00 UTC，目标频带为 1–12 Hz。

| 内容 | 首轮核查 | 条件复核 |
| --- | --- | --- |
| 输入适用性 | 方法输入条件满足 | 方法输入条件满足 |
| 到时检查频带 | 1–8、3–12、5–20 Hz | 3–12 Hz |
| 有效到时配置 | 1/13 | 1/5 |
| 稳定性判断 | 有效配置不足 | 有效配置不足 |
| 观测来源 | 取得的三分量波形 | 复用首轮波形 |

方法输入条件满足与到时稳定性是两个不同判断。现有示例不支持稳定到时结论，也不据此确认地震或识别人工 P/S 震相。正常、边界与失败结果见[验证结果](docs/验证结果.md)。

## 项目资料

- [项目说明](docs/项目说明.md) · [PDF](docs/项目说明.pdf)
- [运行指南](docs/运行指南.md)
- [模型与数据来源](docs/模型与数据来源.md)
- [验证结果](docs/验证结果.md)
- [参赛信息](docs/参赛信息.md) · [独立完成说明](docs/独立完成说明.md)
- [作品下载](https://github.com/BAOXIAOPAI/zhenji-earthquake-validation/releases/tag/submission-2026-10-14)

## 源码

`main/` 包含本地服务、核查流程与前端；`analysis/` 包含观测获取、质量分析与运行配置；`replay/` 保存真实核查记录；`evidence/` 保存验证结果与截图。Agnes Harness 的源码、锁定依赖及 [Apache-2.0 许可证](vendor/agnes-harness/LICENSE) 位于 `vendor/agnes-harness/`。

本作品用于指定观测范围内的方法适用性与到时诊断，不测定震级、不做定位或预警。模型、组件与数据的来源及使用方式见项目资料。
