# DAG 执行器

`DAGExecutor.from_match(task, match)` 接收已细化的 `SpecificationTask` 或旧版 `TaskRepresentation`。`DAGExecutor.from_execution_graph(graph)` 可以直接接收独立组件图。两者均重新核对节点绑定、端口边、OCS/OPS 契约和运行参数。调用方按组件图的入口节点 ID 提供 `DataPacket`；结果包含各节点输出、出口输出、执行顺序及运行指标。

新业务规格图的匹配结果提供 `execution_graph`，其中 `nodes` 包含 CID、参数和端口格式，`edges` 包含源与目标端口，`boundaries` 标出外部入口和出口，`topological_order` 是执行顺序。请求多个解时，`candidate_execution_graphs` 为每组候选绑定提供相同结构。这个图可以独立用于检查装配，无需再从原业务图推断端口接线。

数据链接收演示从外部请求开始，经过细化和静态匹配，再把 500 字节报文编码为 32 个已对齐 OFDM 帧，送入 C15 → C14 → C17 → C35 → C36：

```powershell
python -m examples.data_link_receive_execution_demo
```

单帧调用 `execute({入口节点: 数据包})` 会新建组件，隔离上次运行状态。跨帧重组应使用 `execute_stream(帧输入迭代器)`；它在一次会话中复用组件实例。未收齐分片时，C36 输出空的 `data/message` 包，元数据状态为 `pending`；收齐后输出完整字节载荷和 `delivered` 状态。CRC 错误、帧头冲突或输入端口不完整会报错。

当前接收方案的前提是 OFDM 帧已同步、频偏已校正且信道已均衡或为理想信道。`delivered_message_records` 已覆盖；长期链路交付统计、真实硬件传输和任务 QoS 验收尚不在此方案内。双输入组件通过命名端口和 `process_inputs()` 汇聚分支；现有执行器仍以单输出数据包作为每个节点的结果。
