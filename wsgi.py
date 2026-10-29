from werkzeug.middleware.proxy_fix import ProxyFix

from rd_import.scheduler import configure_logging
from rd_web.app import create_app


configure_logging()
application = create_app()
application.wsgi_app = ProxyFix(application.wsgi_app, x_for=1, x_proto=1, x_host=1)
