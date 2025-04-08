# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2023-2024 @ CAMEL-AI.org. All Rights Reserved. =========
import os
import warnings
from typing import Any, Dict, List, Optional, Type, Union

from openai import AsyncOpenAI, AsyncStream, OpenAI, Stream
from pydantic import BaseModel

from camel.configs import OPENAI_API_PARAMS, ChatGPTConfig
from camel.messages import OpenAIMessage
from camel.models import BaseModelBackend
from camel.types import (
    ChatCompletion,
    ChatCompletionChunk,
    ModelType,
)
from camel.utils import (
    BaseTokenCounter,
    OpenAITokenCounter,
    api_keys_required,
)

UNSUPPORTED_PARAMS = {
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "logit_bias",
}


class OpenAIModel(BaseModelBackend):
    r"""OpenAI API in a unified BaseModelBackend interface.

    Args:
        model_type (Union[ModelType, str]): Model for which a backend is
            created, one of GPT_* series.
        model_config_dict (Optional[Dict[str, Any]], optional): A dictionary
            that will be fed into:obj:`openai.ChatCompletion.create()`. If
            :obj:`None`, :obj:`ChatGPTConfig().as_dict()` will be used.
            (default: :obj:`None`)
        api_key (Optional[str], optional): The API key for authenticating
            with the OpenAI service. (default: :obj:`None`)
        url (Optional[str], optional): The url to the OpenAI service.
            (default: :obj:`None`)
        token_counter (Optional[BaseTokenCounter], optional): Token counter to
            use for the model. If not provided, :obj:`OpenAITokenCounter` will
            be used. (default: :obj:`None`)
        timeout (Optional[float], optional): The timeout value in seconds for
            API calls. If not provided, will fall back to the MODEL_TIMEOUT
            environment variable or default to 180 seconds.
            (default: :obj:`None`)
    """

    @api_keys_required(
        [
            ("api_key", "OPENAI_API_KEY"),
        ]
    )
    def __init__(
        self,
        model_type: Union[ModelType, str],
        model_config_dict: Optional[Dict[str, Any]] = None,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        token_counter: Optional[BaseTokenCounter] = None,
        timeout: Optional[float] = None,
    ) -> None:
        if model_config_dict is None:
            model_config_dict = ChatGPTConfig().as_dict()
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        url = url or os.environ.get("OPENAI_API_BASE_URL")
        timeout = timeout or float(os.environ.get("MODEL_TIMEOUT", 180))

        super().__init__(
            model_type, model_config_dict, api_key, url, token_counter, timeout
        )

        self._client = OpenAI(
            timeout=self._timeout,
            max_retries=3,
            base_url=self._url,
            api_key=self._api_key,
        )
        self._async_client = AsyncOpenAI(
            timeout=self._timeout,
            max_retries=3,
            base_url=self._url,
            api_key=self._api_key,
        )

    def _sanitize_config(self, config_dict: Dict[str, Any]) -> Dict[str, Any]:
        r"""Sanitize the model configuration for O1 models."""

        if self.model_type in [
            ModelType.O1,
            ModelType.O1_MINI,
            ModelType.O1_PREVIEW,
            ModelType.O3_MINI,
        ]:
            warnings.warn(
                "Warning: You are using an reasoning model (O1 or O3), "
                "which has certain limitations, reference: "
                "`https://platform.openai.com/docs/guides/reasoning`.",
                UserWarning,
            )
            return {
                k: v
                for k, v in config_dict.items()
                if k not in UNSUPPORTED_PARAMS
            }
        return config_dict

    def _adapt_messages_for_o1_models(
        self, messages: List[OpenAIMessage]
    ) -> List[OpenAIMessage]:
        r"""Adjust message roles to comply with O1 model requirements by
        converting 'system' or 'developer' to 'user' role.

        Args:
            messages (List[OpenAIMessage]): Message list with the chat history
                in OpenAI API format.

        Returns:
            processed_messages (List[OpenAIMessage]): Return a new list of
                messages to avoid mutating input.
        """

        # Define supported O1 model types as a class constant would be better
        O1_MODEL_TYPES = {ModelType.O1_MINI, ModelType.O1_PREVIEW}

        if self.model_type not in O1_MODEL_TYPES:
            return messages.copy()

        # Issue warning only once using class state
        if not hasattr(self, "_o1_warning_issued"):
            warnings.warn(
                "O1 models (O1_MINI/O1_PREVIEW) have role limitations: "
                "System or Developer messages will be converted to user role."
                "Reference: https://community.openai.com/t/"
                "developer-role-not-accepted-for-o1-o1-mini-o3-mini/1110750/7",
                UserWarning,
                stacklevel=2,
            )
            self._o1_warning_issued = True

        # Create new message list to avoid mutating input
        processed_messages = []
        for message in messages:
            processed_message = message.copy()
            if (
                processed_message["role"] == "system"
                or processed_message["role"] == "developer"
            ):
                processed_message["role"] = "user"  # type: ignore[arg-type]
            processed_messages.append(processed_message)

        return processed_messages

    @property
    def token_counter(self) -> BaseTokenCounter:
        r"""Initialize the token counter for the model backend.

        Returns:
            BaseTokenCounter: The token counter following the model's
                tokenization style.
        """
        if not self._token_counter:
            self._token_counter = OpenAITokenCounter(self.model_type)
        return self._token_counter

    def _run(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[ChatCompletion, Stream[ChatCompletionChunk]]:
        r"""Runs inference of OpenAI chat completion.

        Args:
            messages (List[OpenAIMessage]): Message list with the chat history
                in OpenAI API format.
            response_format (Optional[Type[BaseModel]]): The format of the
                response.
            tools (Optional[List[Dict[str, Any]]]): The schema of the tools to
                use for the request.

        Returns:
            Union[ChatCompletion, Stream[ChatCompletionChunk]]:
                `ChatCompletion` in the non-stream mode, or
                `Stream[ChatCompletionChunk]` in the stream mode.
        """
        messages = self._adapt_messages_for_o1_models(messages)
        response_format = response_format or self.model_config_dict.get(
            "response_format", None
        )
        if response_format:
            return self._request_parse(messages, response_format, tools)
        else:
            return self._request_chat_completion(messages, tools)

    async def _arun(
        self,
        messages: List[OpenAIMessage],
        response_format: Optional[Type[BaseModel]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]:
        r"""Runs inference of OpenAI chat completion in async mode.

        Args:
            messages (List[OpenAIMessage]): Message list with the chat history
                in OpenAI API format.
            response_format (Optional[Type[BaseModel]]): The format of the
                response.
            tools (Optional[List[Dict[str, Any]]]): The schema of the tools to
                use for the request.

        Returns:
            Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]:
                `ChatCompletion` in the non-stream mode, or
                `AsyncStream[ChatCompletionChunk]` in the stream mode.
        """
        response_format = response_format or self.model_config_dict.get(
            "response_format", None
        )
        if response_format:
            return await self._arequest_parse(messages, response_format, tools)
        else:
            return await self._arequest_chat_completion(messages, tools)

    def _request_chat_completion(
        self,
        messages: List[OpenAIMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[ChatCompletion, Stream[ChatCompletionChunk]]:
        import copy

        request_config = copy.deepcopy(self.model_config_dict)

        if tools:
            request_config["tools"] = tools

        request_config = self._sanitize_config(request_config)

        return self._client.chat.completions.create(
            messages=messages,
            model=self.model_type,
            **request_config,
        )

    async def _arequest_chat_completion(
        self,
        messages: List[OpenAIMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]:
        import copy

        request_config = copy.deepcopy(self.model_config_dict)

        if tools:
            request_config["tools"] = tools

        request_config = self._sanitize_config(request_config)

        return await self._async_client.chat.completions.create(
            messages=messages,
            model=self.model_type,
            **request_config,
        )

    def _request_parse(
        self,
        messages: List[OpenAIMessage],
        response_format: Type[BaseModel],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        import copy

        request_config = copy.deepcopy(self.model_config_dict)

        request_config["response_format"] = response_format
        # Remove stream from request config since OpenAI does not support it
        # with structured response
        request_config.pop("stream", None)
        if tools is not None:
            request_config["tools"] = tools

        request_config = self._sanitize_config(request_config)

        return self._client.beta.chat.completions.parse(
            messages=messages,
            model=self.model_type,
            **request_config,
        )

    async def _arequest_parse(
        self,
        messages: List[OpenAIMessage],
        response_format: Type[BaseModel],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> ChatCompletion:
        import copy

        request_config = copy.deepcopy(self.model_config_dict)

        request_config["response_format"] = response_format
        # Remove stream from request config since OpenAI does not support it
        # with structured response
        request_config.pop("stream", None)
        if tools is not None:
            request_config["tools"] = tools

        request_config = self._sanitize_config(request_config)

        return await self._async_client.beta.chat.completions.parse(
            messages=messages,
            model=self.model_type,
            **request_config,
        )

    def check_model_config(self):
        r"""Check whether the model configuration contains any
        unexpected arguments to OpenAI API.

        Raises:
            ValueError: If the model configuration dictionary contains any
                unexpected arguments to OpenAI API.
        """
        for param in self.model_config_dict:
            if param not in OPENAI_API_PARAMS:
                raise ValueError(
                    f"Unexpected argument `{param}` is "
                    "input into OpenAI model backend."
                )

    @property
    def stream(self) -> bool:
        r"""Returns whether the model is in stream mode, which sends partial
        results each time.

        Returns:
            bool: Whether the model is in stream mode.
        """
        return self.model_config_dict.get('stream', False)
