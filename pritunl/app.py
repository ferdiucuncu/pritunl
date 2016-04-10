from pritunl.constants import *
from pritunl.exceptions import *
from pritunl.helpers import *
from pritunl import logger
from pritunl import settings
from pritunl import wsgiserver
from pritunl import limiter
from pritunl import utils

import threading
import flask
import logging
import logging.handlers
import time
import urlparse
import requests
import subprocess
import os

try:
    import OpenSSL
    from pritunl.wsgiserver import ssl_pyopenssl
    SSLAdapter = ssl_pyopenssl.pyOpenSSLAdapter
except ImportError:
    from pritunl.wsgiserver import ssl_builtin
    SSLAdapter = ssl_builtin.BuiltinSSLAdapter

app = flask.Flask(__name__)
app_server = None
redirect_app = flask.Flask(__name__ + '_redirect')
acme_token = None
acme_authorization = None
_cur_cert = None
_cur_key = None
_cur_port = None
_update_lock = threading.Lock()
_watch_event = threading.Event()

def set_acme(token, authorization):
    global acme_token
    global acme_authorization
    acme_token = token
    acme_authorization = authorization

def update_server(delay=0):
    global _cur_cert
    global _cur_key
    global _cur_port

    if not settings.local.server_ready.is_set():
        return

    _update_lock.acquire()
    try:
        if _cur_cert != settings.app.server_cert or \
                _cur_key != settings.app.server_key or \
                _cur_port != settings.app.server_port:
            _cur_cert = settings.app.server_cert
            _cur_key = settings.app.server_key
            _cur_port = settings.app.server_port
            restart_server(delay=delay)
    finally:
        _update_lock.release()

def restart_server(delay=0):
    _watch_event.clear()
    def thread_func():
        time.sleep(delay)
        set_app_server_interrupt()
        if app_server:
            app_server.interrupt = ServerRestart('Restart')
        time.sleep(1)
        clear_app_server_interrupt()
    thread = threading.Thread(target=thread_func)
    thread.daemon = True
    thread.start()

@app.before_request
def before_request():
    flask.g.query_count = 0
    flask.g.write_count = 0
    flask.g.query_time = 0
    flask.g.start = time.time()

@app.after_request
def after_request(response):
    response.headers.add('Execution-Time',
        int((time.time() - flask.g.start) * 1000))
    response.headers.add('Query-Time',
        int(flask.g.query_time * 1000))
    response.headers.add('Query-Count', flask.g.query_count)
    response.headers.add('Write-Count', flask.g.write_count)
    return response

@redirect_app.after_request
def redirect_after_request(response):
    url = list(urlparse.urlsplit(flask.request.url))

    if flask.request.path.startswith('/.well-known/acme-challenge/'):
        return response

    if settings.app.server_ssl:
        url[0] = 'https'
    else:
        url[0] = 'http'
    if settings.app.server_port != 443:
        url[1] += ':%s' % settings.app.server_port
    url = urlparse.urlunsplit(url)
    return flask.redirect(url)

@redirect_app.route('/.well-known/acme-challenge/<token>', methods=['GET'])
def acme_token_get(token):
    if token == acme_token:
        return flask.Response(acme_authorization, mimetype='text/plain')
    return flask.abort(404)

def _run_redirect_wsgi():
    logger.info('Starting redirect server', 'app')

    server = limiter.CherryPyWSGIServerLimited(
        (settings.conf.bind_addr, 80),
        redirect_app,
        server_name=APP_NAME,
    )

    try:
        server.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    except:
        logger.exception('Redirect server error occurred', 'app')
        raise

def _run_server(restart):
    global app_server

    logger.info('Starting server', 'app')

    app_server = limiter.CherryPyWSGIServerLimited(
        ('localhost', settings.app.server_internal_port),
        app,
        request_queue_size=settings.app.request_queue_size,
        server_name=APP_NAME,
        numthreads=settings.app.request_thread_count,
        shutdown_timeout=3,
    )

    server_cert_path = None
    server_key_path = None
    redirect_server = 'true' if settings.app.redirect_server else 'false'
    internal_addr = 'localhost:' + str(settings.app.server_internal_port)

    if settings.app.server_ssl:
        setup_server_cert()

        server_cert_path, server_key_path = utils.write_server_cert(
            settings.app.server_cert,
            settings.app.server_key,
            settings.app.acme_domain,
        )

    process_state = True
    process = subprocess.Popen(
        ['pritunl-web'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(os.environ, **{
            'REDIRECT_SERVER': redirect_server,
            'BIND_HOST': settings.conf.bind_addr,
            'BIND_PORT': str(settings.app.server_port),
            'INTERNAL_ADDRESS': internal_addr,
            'CERT_PATH': server_cert_path or '',
            'KEY_PATH': server_key_path or '',
        }),
    )

    def poll_thread():
        if process.wait() and process_state:
            stdout, stderr = process._communicate(None)
            logger.error("Web server process exited unexpectedly", "app",
                stdout=stdout,
                stderr=stderr,
            )
            time.sleep(1)
            restart_server(1)
    thread = threading.Thread(target=poll_thread)
    thread.daemon = True
    thread.start()

    if not restart:
        settings.local.server_ready.set()
        settings.local.server_start.wait()

    _watch_event.set()

    try:
        app_server.start()
    except (KeyboardInterrupt, SystemExit):
        return
    except ServerRestart:
        raise
    except:
        logger.exception('Server error occurred', 'app')
        raise
    finally:
        process_state = False
        try:
            process.kill()
        except:
            pass

def _run_wsgi():
    restart = False
    while True:
        try:
            _run_server(restart)
        except ServerRestart:
            restart = True
            logger.info('Server restarting...', 'app')
            continue

def _run_wsgi_debug():
    logger.info('Starting debug server', 'app')

    # App.run server uses werkzeug logger
    werkzeug_logger = logging.getLogger('werkzeug')
    werkzeug_logger.setLevel(logging.WARNING)
    werkzeug_logger.addFilter(logger.log_filter)
    werkzeug_logger.addHandler(logger.log_handler)

    settings.local.server_ready.set()
    settings.local.server_start.wait()

    try:
        app.run(
            host=settings.conf.bind_addr,
            port=settings.app.server_port,
            threaded=True,
        )
    except (KeyboardInterrupt, SystemExit):
        pass
    except:
        logger.exception('Server error occurred', 'app')
        raise

def setup_server_cert():
    if not settings.app.server_dh_params:
        utils.create_server_dh_params()
        settings.commit()

    if not settings.app.server_cert or not settings.app.server_key:
        utils.create_server_cert()
        settings.commit()

@interrupter
def _web_watch_thread():
    if settings.app.demo_mode:
        return

    yield interrupter_sleep(5)

    error_count = 0
    while True:
        while True:
            if not _watch_event.wait(0.5):
                error_count = 0
                yield
                continue

            url = ''
            if settings.app.server_ssl:
                verify = False
                url += 'https://'
            else:
                url += 'http://'
                verify = True
            url += 'localhost:%s/ping' % settings.app.server_port

            try:
                resp = requests.get(
                    url,
                    timeout=settings.app.server_watch_timeout,
                    verify=verify,
                )

                if resp.status_code != 200 and _watch_event.is_set():
                    logger.error('Failed to ping web server, bad status',
                        'watch',
                        url=url,
                        status_code=resp.status_code,
                        content=resp.content,
                    )
                    break
            except:
                if _watch_event.is_set():
                    logger.exception('Failed to ping web server', 'watch',
                        url=url,
                    )
                    break

            error_count = 0
            yield interrupter_sleep(3)

        error_count += 1
        if error_count > 1:
            error_count = 0
            logger.error('Web server non-responsive, restarting...', 'watch')
            restart_server()
            yield interrupter_sleep(10)
        else:
            yield interrupter_sleep(2)

def start_web_watch():
    threading.Thread(target=_web_watch_thread).start()

def run_server():
    global _cur_cert
    global _cur_key
    global _cur_port
    _cur_cert = settings.app.server_cert
    _cur_key = settings.app.server_key
    _cur_port = settings.app.server_port

    if settings.conf.debug:
        logger.LogEntry(message='Web debug server started.')
    else:
        logger.LogEntry(message='Web server started.')

    if settings.conf.debug:
        _run_wsgi_debug()
    else:
        if settings.app.server_watch:
            start_web_watch()
        _run_wsgi()
