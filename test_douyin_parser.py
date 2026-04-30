import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

if "utils" not in sys.modules:
    utils_module = types.ModuleType("utils")
    utils_module.__path__ = []
    sys.modules["utils"] = utils_module

if "utils.decorators" not in sys.modules:
    decorators = types.ModuleType("utils.decorators")

    def on_text_message(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    decorators.on_text_message = on_text_message
    sys.modules["utils.decorators"] = decorators

if "utils.plugin_base" not in sys.modules:
    plugin_base = types.ModuleType("utils.plugin_base")

    class PluginBase:
        pass

    plugin_base.PluginBase = PluginBase
    sys.modules["utils.plugin_base"] = plugin_base

if "WechatAPI" not in sys.modules:
    wechat_api = types.ModuleType("WechatAPI")

    class WechatAPIClient:
        pass

    wechat_api.WechatAPIClient = WechatAPIClient
    sys.modules["WechatAPI"] = wechat_api

try:
    from plugins.DouyinParser.main import DouyinParser
except ModuleNotFoundError:
    from main import DouyinParser


def test_parse_note_page_from_router_data():
    html = """
    <html>
      <body>
        <script>
          window._ROUTER_DATA = {
            "loaderData": {
              "note_(id)/page": {
                "videoInfoRes": {
                  "item_list": [
                    {
                      "desc": "ChatGPT Image2.0 提示词。这是一张老式数码相机拍摄的快照",
                      "author": {"nickname": "河马实验室"},
                      "images": [
                        {
                          "url_list": [
                            "https://p26-sign.douyinpic.com/example.webp",
                            "https://p3-sign.douyinpic.com/example.jpeg"
                          ]
                        }
                      ],
                      "video": {
                        "play_addr": {
                          "url_list": [
                            "https://aweme.snssdk.com/aweme/v1/playwm/?video_id=music.mp3"
                          ]
                        },
                        "duration": 0
                      }
                    }
                  ]
                }
              }
            },
            "errors": null
          }
        </script>
      </body>
    </html>
    """

    result = DouyinParser._parse_page_html(html)

    assert result["kind"] == "note"
    assert result["title"] == "ChatGPT Image2.0 提示词。这是一张老式数码相机拍摄的快照"
    assert result["author"] == "河马实验室"
    assert result["image_url"] == "https://p3-sign.douyinpic.com/example.jpeg"
    assert "playwm" not in result.get("url", "")
