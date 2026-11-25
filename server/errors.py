from fastapi.responses import JSONResponse
from uuid import uuid4


class DomainError(Exception):
    def __init__(self, code, status=422, message='输入无效，请检查后重试', retryable=False, details=None):
        self.code, self.status, self.message = code, status, message
        self.retryable, self.details = retryable, details or {}

    def dto(self):
        return dict(code=self.code,message=self.message,retryable=self.retryable,details=self.details)


def response(data, status=200):
    return JSONResponse({'request_id':str(uuid4()),'data':data},status_code=status)


def error_response(error):
    return JSONResponse({'request_id':str(uuid4()),'error':error.dto()},status_code=error.status,
                        headers={'Retry-After':str(error.details['retry_after_seconds'])} if 'retry_after_seconds' in error.details else {})
