bind = '0.0.0.0:8000'
workers = 2
threads = 4
timeout = 180
graceful_timeout = 30
accesslog = '-'
errorlog = '-'
capture_output = True
access_log_format = ('{"time":"%(t)s","level":"INFO","event":"http_request",'
                     '"remote":"%(h)s","method":"%(m)s","path":"%(U)s",'
                     '"status":%(s)s,"duration_us":%(D)s}')
