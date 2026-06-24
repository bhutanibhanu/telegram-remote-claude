"""P10 T1 — native multimodal image input (engine/adapter/config side).

Covers the send-path threading proven by the spike: an operator-supplied image is carried
as a normalized :class:`~claude_tg.engine.types.ImageInput`, the SDK adapter builds the
``[text, image]`` content-block ``user`` dict and feeds it to ``client.query`` as a
single-item async-iterable, and the optional ``images`` kwarg threads through
``Engine.send`` → ``Substrate.send`` while leaving the text path 100% unchanged. The bot
handler (SB1 / size-cap / media_type / no-bytes-logged) is in test_bot_streaming.py; the
live-verify that Claude actually SEES the pixels is T4.

No live Claude / no network: the SDK client is a fake that records the ``query`` argument.
"""

from __future__ import annotations

import base64

import claude_agent_sdk as sdk
import pytest

from claude_tg.config import DEFAULT_IMAGE_MAX_BYTES, parse_image_max_bytes
from claude_tg.engine import Engine, ImageInput, ResultEvent
from claude_tg.engine.adapter_sdk import SdkSubstrate, _user_message_with_images


async def drain(aiter):
    return [ev async for ev in aiter]


# ---------------------------------------------------------------------------
# The content-block builder (the load-bearing structure; mutation-probed below)
# ---------------------------------------------------------------------------


def test_user_message_block_structure_text_then_image():
    img = ImageInput(data="QkFTRTY0", media_type="image/png")
    msg = _user_message_with_images("what is this?", [img], "sess-7")

    # The envelope is a `user` message dict the SDK streams verbatim.
    assert msg["type"] == "user"
    assert msg["message"]["role"] == "user"
    assert msg["parent_tool_use_id"] is None
    assert msg["session_id"] == "sess-7"

    content = msg["message"]["content"]
    # Block 0 = the caption/prompt TEXT (leads the image); block 1 = the IMAGE.
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"] == {
        "type": "base64",
        "media_type": "image/png",
        "data": "QkFTRTY0",
    }


def test_user_message_block_multiple_images_each_a_block():
    imgs = [
        ImageInput(data="AAAA", media_type="image/jpeg"),
        ImageInput(data="BBBB", media_type="image/webp"),
    ]
    content = _user_message_with_images("caption", imgs, "default")["message"]["content"]
    # text + one image block per ImageInput, in order.
    assert [b["type"] for b in content] == ["text", "image", "image"]
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == "AAAA"
    assert content[2]["source"]["media_type"] == "image/webp"
    assert content[2]["source"]["data"] == "BBBB"


def test_user_message_block_mutation_probe_caption_and_media_type():
    """Mutation-probe: the caption MUST be the text block and the media_type/data MUST ride
    the image source — a swap or a drop is a real defect (the model would see the wrong
    prompt or reject mislabeled bytes). Pins the exact wiring against a transposition."""
    img = ImageInput(data="ZZ99", media_type="image/gif")
    content = _user_message_with_images("PROMPT-X", [img], "s")["message"]["content"]

    # The caption is NOT in the image block, and the base64 is NOT in the text block.
    assert content[0]["text"] == "PROMPT-X"
    assert "text" not in content[1]  # image block has no text key
    assert content[1]["source"]["data"] == "ZZ99"  # data, not the caption
    assert content[1]["source"]["media_type"] == "image/gif"  # carried through verbatim
    # The image source is a base64 source (never raw / url).
    assert content[1]["source"]["type"] == "base64"


def test_image_input_repr_elides_base64_sb3():
    # SB3: ImageInput.repr must NEVER dump the base64 (an accidental log(image) is a leak).
    secret_b64 = base64.b64encode(b"super-secret-pixels-payload").decode("ascii")
    img = ImageInput(data=secret_b64, media_type="image/png")
    text = repr(img)
    assert secret_b64 not in text
    assert "image/png" in text and "b64 chars" in text


# ---------------------------------------------------------------------------
# The SDK adapter: images thread into client.query as an async-iterable user dict
# ---------------------------------------------------------------------------


class _RecordingClient:
    """Fake SDK client that RECORDS what ``query`` was called with, then yields a result."""

    def __init__(self):
        self.query_arg = None
        self.query_was_str = None

    async def connect(self):
        pass

    async def query(self, prompt, session_id="default"):
        self.query_was_str = isinstance(prompt, str)
        if isinstance(prompt, str):
            self.query_arg = prompt
        else:
            # An async-iterable of dicts — materialize it so the test can inspect the dict.
            self.query_arg = [item async for item in prompt]

    def receive_response(self):
        async def _gen():
            yield sdk.ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="S-img",
                total_cost_usd=0.0,
                result="seen",
            )

        return _gen()

    async def disconnect(self):
        pass


async def test_sdk_send_with_images_streams_content_block_user_dict():
    sub = SdkSubstrate()
    sub._client = _RecordingClient()
    img = ImageInput(data="QUJD", media_type="image/jpeg")
    out = await drain(sub.send("describe it", images=[img]))

    # The turn completed normally (the receive loop is identical to the text path).
    assert any(isinstance(e, ResultEvent) for e in out), out
    # query received an ASYNC-ITERABLE (not a str), and it yielded exactly one user dict
    # with the [text, image] content blocks.
    client = sub._client
    assert client.query_was_str is False
    assert len(client.query_arg) == 1
    streamed = client.query_arg[0]
    assert streamed["type"] == "user"
    content = streamed["message"]["content"]
    assert content[0] == {"type": "text", "text": "describe it"}
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert content[1]["source"]["data"] == "QUJD"


async def test_sdk_send_without_images_is_plain_str_query_unchanged():
    # The text path: query gets the bare prompt STRING, exactly as pre-P10 (no image dict).
    sub = SdkSubstrate()
    sub._client = _RecordingClient()
    out = await drain(sub.send("just text"))
    assert any(isinstance(e, ResultEvent) for e in out), out
    assert sub._client.query_was_str is True
    assert sub._client.query_arg == "just text"


async def test_sdk_send_empty_images_list_takes_text_path():
    # An empty list is falsy → the text path (no needless content-block envelope).
    sub = SdkSubstrate()
    sub._client = _RecordingClient()
    await drain(sub.send("hi", images=[]))
    assert sub._client.query_was_str is True


# ---------------------------------------------------------------------------
# Engine.send threads images through to the substrate (text path untouched)
# ---------------------------------------------------------------------------


class _RecordingSubstrate:
    """Records the kwargs each ``send`` was called with; yields one result event."""

    def __init__(self):
        self.session_id = None
        self.send_calls = []

    async def start(self):
        pass

    async def resume(self, session_id):
        self.session_id = session_id

    async def send(self, prompt, *, timeout=120.0, images=None):
        self.send_calls.append({"prompt": prompt, "timeout": timeout, "images": images})
        yield ResultEvent(session_id="S", is_error=False, subtype="success")

    async def stop(self):
        pass


async def test_engine_send_threads_images_to_substrate():
    sub = _RecordingSubstrate()
    eng = Engine(sub)
    img = ImageInput(data="RkZG", media_type="image/png")
    await drain(eng.send("look", images=[img]))
    assert len(sub.send_calls) == 1
    assert sub.send_calls[0]["images"] == [img]
    assert sub.send_calls[0]["prompt"] == "look"


async def test_engine_send_text_path_omits_images_kwarg():
    """The text turn must call the substrate's send with NO ``images`` kwarg, so a legacy
    substrate fake whose ``send`` has no ``images`` parameter keeps working verbatim (the
    whole-suite invariant). We prove it with a fake whose send REJECTS an images kwarg."""

    class _LegacySubstrate:
        session_id = None

        async def start(self):
            pass

        async def resume(self, session_id):
            pass

        async def send(self, prompt, *, timeout=120.0):  # NO images kwarg (pre-P10 shape)
            yield ResultEvent(session_id="S", is_error=False, subtype="success")

        async def stop(self):
            pass

    eng = Engine(_LegacySubstrate())
    # No images → must not pass the kwarg → no TypeError against the legacy signature.
    out = await drain(eng.send("text only"))
    assert any(isinstance(e, ResultEvent) for e in out), out


# ---------------------------------------------------------------------------
# Config: IMAGE_MAX_BYTES parsing/validation
# ---------------------------------------------------------------------------


def test_parse_image_max_bytes_default_and_values():
    assert parse_image_max_bytes(None) == DEFAULT_IMAGE_MAX_BYTES
    assert parse_image_max_bytes("") == DEFAULT_IMAGE_MAX_BYTES
    assert parse_image_max_bytes("   ") == DEFAULT_IMAGE_MAX_BYTES
    assert parse_image_max_bytes("1048576") == 1048576


@pytest.mark.parametrize("bad", ["0", "-1", "x", "1.5"])
def test_parse_image_max_bytes_rejects_bad(bad):
    # 0/negative would reject every image; non-integer is a typo — both fail loud (not
    # silently disable images).
    with pytest.raises(ValueError):
        parse_image_max_bytes(bad)
