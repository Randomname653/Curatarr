import pytest
from src.schemas.chat import ChatMessage, DiscussContext
def test_discuss_context():
    dc = DiscussContext(title="Test", pitch="Because.")
    msg = ChatMessage(message="Hello", discuss_context=dc)
    assert msg.discuss_context.title == "Test"
    assert msg.discuss_context.pitch == "Because."
