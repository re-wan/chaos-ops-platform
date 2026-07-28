"""任务队列名称常量（Phase 3 Step 05）。

集中定义队列名，避免 ``app.core.task_queue`` 与 ``app.workers`` / 业务服务之间
产生循环导入。生产代码与 worker、测试均引用此处常量。
"""

# 指标写入：Agent 上报在异步模式下先入此队列，由 metric_worker 批量落库。
QUEUE_METRIC_WRITE = "metric.write"

# 告警检测：指标落库后入此队列，由 detector_worker 异步评估相关规则。
QUEUE_ALERT_DETECT = "alert.detect"

# 通知发送：send_notification 创建 pending 记录后入此队列，由 notify_worker 投递。
QUEUE_NOTIFY_SEND = "notify.send"

# 单批指标写入上限（对齐 PERFORMANCE_DESIGN §4.1：每批最多 5000 条）。
METRIC_WRITE_BATCH_MAX = 5000
