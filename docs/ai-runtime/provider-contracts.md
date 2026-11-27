# 模型供应商协议

Chat Completions 的成功 HTTP 响应中如果含有值不为 `null` 的 `error` 字段，
同步调用和流式调用都将该响应视为失败。日志和调用方收到固定的错误描述。
流式调用按顺序保留已产生的数据块，并在收到错误时关闭上游连接。
网关发出数据块后，收到这类错误时会发送错误事件并结束当前流。

运行时根据合法的错误状态码或已知的 `code`、`type` 值判断失败类别。
诊断中的 `status_code` 记录实际 HTTP 状态，`effective_status_code`
在两者不同时记录分类状态。实际 HTTP 200 中的 429 对应供应商限流。
嵌入的 5xx 沿用供应商服务端错误的重试判定，实际 HTTP 5xx 才记录为
`provider_http_5xx`。无法识别的错误使用 `provider_error_envelope`
和 `provider_bad_response`。

`error: null` 与普通空结果沿用现有处理。流式输出开始前，
运行时仍按配置判断是否重试或回退。
