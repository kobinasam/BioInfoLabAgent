import os
import time
import json
import tiktoken
import openai

from openai import OpenAI

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import google.generativeai as genai
except ImportError:
    genai = None


TOKENS_IN = {}
TOKENS_OUT = {}
encoding = tiktoken.get_encoding("cl100k_base")


def curr_cost_est():
    costmap_in = {
        "gpt-4o": 2.50 / 1_000_000,
        "gpt-4o-mini": 0.150 / 1_000_000,
        "o1-preview": 15.00 / 1_000_000,
        "o1-mini": 3.00 / 1_000_000,
        "claude-3-5-sonnet": 3.00 / 1_000_000,
        "deepseek-chat": 1.00 / 1_000_000,
        "o1": 15.00 / 1_000_000,
        "o3-mini": 1.10 / 1_000_000,
    }

    costmap_out = {
        "gpt-4o": 10.00 / 1_000_000,
        "gpt-4o-mini": 0.60 / 1_000_000,
        "o1-preview": 60.00 / 1_000_000,
        "o1-mini": 12.00 / 1_000_000,
        "claude-3-5-sonnet": 12.00 / 1_000_000,
        "deepseek-chat": 5.00 / 1_000_000,
        "o1": 60.00 / 1_000_000,
        "o3-mini": 4.40 / 1_000_000,
    }

    total = 0.0
    for model, tokens in TOKENS_IN.items():
        total += costmap_in.get(model, 0.0) * tokens
    for model, tokens in TOKENS_OUT.items():
        total += costmap_out.get(model, 0.0) * tokens
    return total


def _extract_openrouter_model(model_str):
    prefix = "openrouter:"
    if isinstance(model_str, str) and model_str.startswith(prefix):
        return True, model_str[len(prefix):]
    return False, model_str


def _make_openrouter_client(api_key):
    if not api_key:
        raise Exception(
            "No OpenRouter API key provided. Set OPENROUTER_API_KEY "
            "or pass the OpenRouter key through api-key."
        )

    headers = {}
    referer = os.getenv("OPENROUTER_HTTP_REFERER")
    title = os.getenv("OPENROUTER_X_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title

    kwargs = {
        "api_key": api_key,
        # Overridable so the multi-user server can route through its key-holding proxy.
        "base_url": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    }
    if headers:
        kwargs["default_headers"] = headers

    return OpenAI(**kwargs)


def _chat_completion(client, model, system_prompt, prompt, temp=None, combine_system=False):
    if combine_system:
        messages = [{"role": "user", "content": f"{system_prompt}\n{prompt}"}]
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

    kwargs = {"model": model, "messages": messages}
    if temp is not None:
        kwargs["temperature"] = temp

    completion = client.chat.completions.create(**kwargs)
    if not getattr(completion, "choices", None):
        # OpenRouter reports upstream/rate-limit failures as an `error` object
        # (sometimes with HTTP 200), leaving `choices` empty. Surface the real reason.
        err = (getattr(completion, "model_extra", None) or {}).get("error") or {}
        if isinstance(err, dict):
            raise Exception(f"Provider error {err.get('code', '?')}: {err.get('message', 'empty response (no choices)')}")
        raise Exception(f"Provider error: {err or 'empty response (no choices)'}")
    content = completion.choices[0].message.content
    if content is None:
        raise Exception("Provider returned an empty message")
    return content


def _record_tokens(model_key, system_prompt, prompt, answer, print_cost=True):
    try:
        if model_key in [
            "o1-preview",
            "o1-mini",
            "claude-3.5-sonnet",
            "o1",
            "o3-mini",
        ]:
            current_encoding = tiktoken.encoding_for_model("gpt-4o")
        elif model_key == "deepseek-chat" or model_key.startswith("openrouter:"):
            current_encoding = tiktoken.get_encoding("cl100k_base")
        else:
            try:
                current_encoding = tiktoken.encoding_for_model(model_key)
            except Exception:
                current_encoding = tiktoken.get_encoding("cl100k_base")

        if model_key not in TOKENS_IN:
            TOKENS_IN[model_key] = 0
            TOKENS_OUT[model_key] = 0

        TOKENS_IN[model_key] += len(current_encoding.encode(system_prompt + prompt))
        TOKENS_OUT[model_key] += len(current_encoding.encode(answer or ""))

        if print_cost:
            print(
                f"Current experiment cost = ${curr_cost_est():.6f}, "
                "** Approximate values, may not reflect true cost"
            )
    except Exception as e:
        if print_cost:
            print(f"Cost approximation has an error? {e}")


def query_model(
    model_str,
    prompt,
    system_prompt,
    openai_api_key=None,
    gemini_api_key=None,
    anthropic_api_key=None,
    tries=5,
    timeout=5.0,
    temp=None,
    print_cost=True,
    version="1.5",
):
    is_openrouter, openrouter_model = _extract_openrouter_model(model_str)

    preloaded_openai_api = os.getenv("OPENAI_API_KEY")
    preloaded_openrouter_api = os.getenv("OPENROUTER_API_KEY")

    if is_openrouter:
        openrouter_api_key = preloaded_openrouter_api or openai_api_key
    else:
        openrouter_api_key = None
        if openai_api_key is None and preloaded_openai_api is not None:
            openai_api_key = preloaded_openai_api

    if (
        not is_openrouter
        and openai_api_key is None
        and anthropic_api_key is None
        and gemini_api_key is None
    ):
        raise Exception("No API key provided in query_model function")

    if not is_openrouter and openai_api_key is not None:
        openai.api_key = openai_api_key
        os.environ["OPENAI_API_KEY"] = openai_api_key

    if anthropic_api_key is not None:
        os.environ["ANTHROPIC_API_KEY"] = anthropic_api_key

    if gemini_api_key is not None:
        os.environ["GEMINI_API_KEY"] = gemini_api_key

    for _attempt in range(tries):
        try:
            # OpenRouter-compatible models. Configure YAML as:
            # openrouter:nvidia/nemotron-3-ultra-550b-a55b:free
            if is_openrouter:
                client = _make_openrouter_client(openrouter_api_key)
                answer = _chat_completion(
                    client,
                    openrouter_model,
                    system_prompt,
                    prompt,
                    temp=temp,
                    combine_system=False,
                )

            elif model_str in [
                "gpt-4o-mini",
                "gpt4omini",
                "gpt-4omini",
                "gpt4o-mini",
            ]:
                model_str = "gpt-4o-mini"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]

                if version == "0.28":
                    kwargs = {"model": model_str, "messages": messages}
                    if temp is not None:
                        kwargs["temperature"] = temp
                    completion = openai.ChatCompletion.create(**kwargs)
                else:
                    client = OpenAI()
                    kwargs = {
                        "model": "gpt-4o-mini-2024-07-18",
                        "messages": messages,
                    }
                    if temp is not None:
                        kwargs["temperature"] = temp
                    completion = client.chat.completions.create(**kwargs)
                answer = completion.choices[0].message.content

            elif model_str == "gemini-2.0-pro":
                if genai is None:
                    raise ImportError("google-generativeai is not installed.")
                genai.configure(api_key=gemini_api_key)
                model = genai.GenerativeModel(
                    model_name="gemini-2.0-pro-exp-02-05",
                    system_instruction=system_prompt,
                )
                answer = model.generate_content(prompt).text

            elif model_str == "gemini-1.5-pro":
                if genai is None:
                    raise ImportError("google-generativeai is not installed.")
                genai.configure(api_key=gemini_api_key)
                model = genai.GenerativeModel(
                    model_name="gemini-1.5-pro",
                    system_instruction=system_prompt,
                )
                answer = model.generate_content(prompt).text

            elif model_str == "o3-mini":
                model_str = "o3-mini"
                messages = [{"role": "user", "content": system_prompt + prompt}]
                if version == "0.28":
                    completion = openai.ChatCompletion.create(
                        model=model_str,
                        messages=messages,
                    )
                else:
                    client = OpenAI()
                    completion = client.chat.completions.create(
                        model="o3-mini-2025-01-31",
                        messages=messages,
                    )
                answer = completion.choices[0].message.content

            elif model_str == "claude-3.5-sonnet":
                if anthropic is None:
                    raise ImportError("anthropic is not installed.")
                client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                message = client.messages.create(
                    model="claude-3-5-sonnet-latest",
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                answer = json.loads(message.to_json())["content"][0]["text"]

            elif model_str in ["gpt4o", "gpt-4o"]:
                model_str = "gpt-4o"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                if version == "0.28":
                    kwargs = {"model": model_str, "messages": messages}
                    if temp is not None:
                        kwargs["temperature"] = temp
                    completion = openai.ChatCompletion.create(**kwargs)
                else:
                    client = OpenAI()
                    kwargs = {
                        "model": "gpt-4o-2024-08-06",
                        "messages": messages,
                    }
                    if temp is not None:
                        kwargs["temperature"] = temp
                    completion = client.chat.completions.create(**kwargs)
                answer = completion.choices[0].message.content

            elif model_str == "deepseek-chat":
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ]
                if version == "0.28":
                    raise Exception("Please upgrade your OpenAI version to use DeepSeek client")

                deepseek_client = OpenAI(
                    api_key=os.getenv("DEEPSEEK_API_KEY"),
                    base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
                )
                kwargs = {"model": "deepseek-chat", "messages": messages}
                if temp is not None:
                    kwargs["temperature"] = temp
                completion = deepseek_client.chat.completions.create(**kwargs)
                answer = completion.choices[0].message.content

            elif model_str == "o1-mini":
                model_str = "o1-mini"
                messages = [{"role": "user", "content": system_prompt + prompt}]
                if version == "0.28":
                    completion = openai.ChatCompletion.create(
                        model=model_str,
                        messages=messages,
                    )
                else:
                    client = OpenAI()
                    completion = client.chat.completions.create(
                        model="o1-mini-2024-09-12",
                        messages=messages,
                    )
                answer = completion.choices[0].message.content

            elif model_str == "o1":
                model_str = "o1"
                messages = [{"role": "user", "content": system_prompt + prompt}]
                if version == "0.28":
                    completion = openai.ChatCompletion.create(
                        model="o1-2024-12-17",
                        messages=messages,
                    )
                else:
                    client = OpenAI()
                    completion = client.chat.completions.create(
                        model="o1-2024-12-17",
                        messages=messages,
                    )
                answer = completion.choices[0].message.content

            elif model_str == "o1-preview":
                model_str = "o1-preview"
                messages = [{"role": "user", "content": system_prompt + prompt}]
                if version == "0.28":
                    completion = openai.ChatCompletion.create(
                        model=model_str,
                        messages=messages,
                    )
                else:
                    client = OpenAI()
                    completion = client.chat.completions.create(
                        model="o1-preview",
                        messages=messages,
                    )
                answer = completion.choices[0].message.content

            else:
                raise ValueError(
                    f"Unsupported model backend: {model_str}. "
                    "For OpenRouter use 'openrouter:<MODEL_ID>'."
                )

            model_key = (
                f"openrouter:{openrouter_model}"
                if is_openrouter
                else model_str
            )
            _record_tokens(
                model_key,
                system_prompt,
                prompt,
                answer,
                print_cost=print_cost,
            )
            return answer

        except Exception as e:
            print("Inference Exception:", e)
            msg = str(e).lower()
            if "429" in msg or "rate" in msg:
                # free-tier rate limits need a real back-off, not a fixed 5 s
                wait = min(60.0, timeout * (2 ** (_attempt + 1)))
                print(f"Rate limited; waiting {wait:.0f}s before retrying")
                time.sleep(wait)
            else:
                time.sleep(timeout)

    raise Exception("Max retries: timeout")


# Example:
# print(
#     query_model(
#         model_str="openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
#         prompt="""Solve 12 * 13 and provide the final answer.""",
#         system_prompt="""You are a helpful mathematician.""",
#         openai_api_key=os.getenv("OPENROUTER_API_KEY"),
#         temp=0.2,
#     )
# )
