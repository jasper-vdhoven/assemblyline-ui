from assemblyline.common.str_utils import safe_str
import requests
import yaml

from assemblyline_ui.config import config, LOGGER


class AiApiException(Exception):
    pass


def _call_ai_backend(data, system_message, action):
    # Build chat completions request
    data = {
        "max_tokens": config.ui.ai.max_tokens,
        "messages": [
            {"role": "system", "content": system_message},
            {"role": "user", "content": data},
        ],
        "model": config.ui.ai.model_name,
        "stream": False
    }
    data.update(config.ui.ai.options)

    try:
        # Call API
        resp = requests.post(config.ui.ai.chat_url, headers=config.ui.ai.headers, json=data)
    except Exception as e:
        message = f"An exception occured while trying to {action} with AI on server {config.ui.ai.chat_url}. [{e}]"
        LOGGER.warning(message)
        raise AiApiException(message)

    if not resp.ok:
        msg_data = resp.json()
        msg = msg_data.get('error', {}).get('message', None) or msg_data
        message = f"The AI API denied the request to {action} with the following message: {msg}"
        LOGGER.warning(message)
        raise AiApiException(message)

    # Get AI responses
    responses = resp.json().get('choices', [])
    if responses:
        content = responses[0].get('message', {}).get('content', None)
        return content or None

    return None


def summarized_al_submission(report):
    return _call_ai_backend(yaml.dump(report), config.ui.ai.report_system_message, "summarize the AL report")


def summarize_code_snippet(code):
    return _call_ai_backend(safe_str(code), config.ui.ai.code_system_message, "summarize code snippet")
