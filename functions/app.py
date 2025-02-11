import sys
import os
from chat import app as flask_app
from serverless_wsgi import handle_request

def handler(event, context):
    return handle_request(flask_app, event, context)
