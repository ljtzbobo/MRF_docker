# 合成接口测试请求

本目录保留历史名称 External，表示使用外部业务请求接口格式；不表示内容来自真实采集或真实任务。

| 文件 | 定位 |
| --- | --- |
| `01_voice_communication.json` | 合成接口测试：语音请求 |
| `02_data_link_transfer.json` | 合成接口测试：数据链请求 |
| `03_target_search.json` | 合成接口测试：搜索请求 |
| `04_target_tracking.json` | 合成接口测试：跟踪请求 |
| `05_signal_interception.json` | 合成接口测试：截获请求 |
| `06_signal_identification.json` | 合成接口测试：识别请求 |
| `07_spectrum_analysis.json` | 合成接口测试：频谱分析请求 |
| `all_tasks.json` | 上述合成请求的批量测试合集 |

这些文件用于解析、任务表征、细化、匹配和 API 回归测试，不是任务数据集、实采 IQ、真实雷达回波或真实参考标签。
不计入真实数据集的样本数，不以其执行结果证明真实平台的性能。
JSON 内容和路径保留兼容现有代码；真实数据的存放和接入规范见 [datasets](../../../datasets/README.md)。
