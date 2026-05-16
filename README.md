# DouyinParser 插件

这是一个用于 869WXbot/XYBot 的抖音分享解析插件。

## 功能

- 自动识别群聊中的抖音分享链接。
- 视频作品：解析后发送抖音视频卡片。
- 图文作品：解析后抓取全部图片，按顺序拼接成长图发送，并追加作者和文案文本。
- 支持 `allowed_groups = ["*"]` 或指定群聊白名单。

## 配置

在 `plugins/DouyinParser/config.toml` 中配置：

```toml
[DouyinParser]
enable = true
allowed_groups = ["group_id1@chatroom", "group_id2@chatroom"]
```

## 使用

在白名单群聊中发送抖音分享文本即可，例如：

```text
复制打开抖音，看看【作者的图文作品】文案内容 https://v.douyin.com/xxxx/
```

插件会自动解析链接：

- 如果是视频，发送视频卡片。
- 如果是图文，发送 1 张按原图顺序拼接的长图和对应文案。

如果想引用卡片并下载视频，请启用独立的 `DouyinDownloader` 插件。

## 验证

插件内包含图文解析回归测试：

```bash
python3 -m pytest plugins/DouyinParser/test_douyin_parser.py -q -o addopts=
```

如果在独立插件仓库中运行：

```bash
python3 -m pytest test_douyin_parser.py -q -o addopts=
```
