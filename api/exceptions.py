try:
    from requests.exceptions import JSONDecodeError
except ImportError:
    from json import JSONDecodeError


class LoginError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class InputFormatError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class MaxRollBackExceeded(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class MaxRetryExceeded(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)


class StudyRequestError(Exception):
    """任务点请求失败且重试已耗尽（状态码异常 / 网络异常 / 响应不是合法 JSON）。

    抛出后由上层（``engine.process_job``）收敛成 ``StudyResult.ERROR``，
    绝不让单点网络/解析异常穿透到刷课主循环导致整轮中止。
    """

    def __init__(self, *args: object):
        super().__init__(*args)


class FontDecodeError(Exception):
    def __init__(self, *args: object):
        super().__init__(*args)
