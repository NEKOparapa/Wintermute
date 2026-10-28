"""Runner 抛出的异常；工具错误会作为结果返回给模型。"""


class AgentError(Exception):
    """智能体执行调度异常的基类。"""


class AgentBusyError(AgentError):
    """Runner 已有正在执行的任务。"""


class MaxTurnsExceeded(AgentError):
    """模型调用次数已达到配置的上限。"""


class ModelProtocolError(AgentError):
    """模型输出流违反了完整消息协议。"""
