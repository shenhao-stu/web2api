"""插件层公共异常，独立模块避免循环导入。"""


class AccountFrozenError(RuntimeError):
    """
    插件在检测到账号被限流/额度用尽时抛出，携带解冻时间戳（Unix 秒）。
    由 chat_handler 捕获后写入配置并重试其他账号。
    """

    def __init__(self, message: str, unfreeze_at: int) -> None:
        super().__init__(message)
        self.unfreeze_at = unfreeze_at


class BrowserResourceInvalidError(RuntimeError):
    """页面 / tab / browser 资源失效时抛出，供上层做定向回收与重试。"""

    def __init__(
        self,
        detail: str,
        *,
        helper_name: str,
        operation: str,
        stage: str,
        resource_hint: str,
        request_url: str,
        page_url: str,
        request_id: str | None = None,
        stream_phase: str | None = None,
        proxy_key: object | None = None,
        type_name: str | None = None,
        account_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.helper_name = helper_name
        self.operation = operation
        self.stage = stage
        self.resource_hint = resource_hint
        self.request_url = request_url
        self.page_url = page_url
        self.request_id = request_id
        self.stream_phase = stream_phase
        self.proxy_key = proxy_key
        self.type_name = type_name
        self.account_id = account_id
