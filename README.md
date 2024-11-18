# bsky-feeder

This is a small implementation of an AI powered BlueSky feed generator.

built with `atproto`, `fastapi`, `mlx` and `sqlite` to create a feed of posts that are about `"Science"` as classified by the `Qwen2-7B-Instruct` model.

## Processing Pipeline

![run-bksy-feeder](https://github.com/user-attachments/assets/f88b07ed-254b-4638-a1c0-6723dd0b55e8)

Start the server/feed processor with the following command:

```bash
uv run server.py
```

This will start a server on port `8000` as well as listen for new BlueSky posts.

As new posts are received, the server will pass the text into a local LLM run on MLX (on my M3 MBP).

The LLM will classify the text as "yes" or "no" based on whether the text meets some criteria.

For posts that are classified as "yes", the server will store these in a local SQLite database.

## Consuming the Feed

We can see the feed by calling the `describeFeedGenerator` method:

```bash
curl -s http://localhost:8000/xrpc/app.bsky.feed.describeFeedGenerator | jq
# {
#     "did": "did:web:feed.example.com",
#     "feeds": [
#         {
#             "uri": "at://did:web:feed.example.com/app.bsky.feed.generator/drbh-feed",
#             "name": "drbhs Feed",
#             "description": "A feed of posts that meet the criteria",
#         }
#     ],
# }
```

To consume the feed, you can use the following command:

```bash
curl -s http://localhost:8000/xrpc/app.bsky.feed.getFeedSkeleton\?feed\=at://did:web:feed.example.com/app.bsky.feed.generator/drbh-feed | jq
# {
#     "cursor": "at://did:plc:ffffffffffffffffffffffff/app.bsky.feed.post/abcd",
#     "feed": [
#         {
#             "post": "at://did:plc:ffffffffffffffffffffffff/app.bsky.feed.post/abcd1"
#         },
#         {
#             "post": "at://did:plc:ffffffffffffffffffffffff/app.bsky.feed.post/abcd2"
#         },
#         {
#             "post": "at://did:plc:ffffffffffffffffffffffff/app.bsky.feed.post/abcd3"
#         },
#         ...,
#     ],
# }
```

then you can resolve the post via a public resolver:

```bash
curl -s https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts\?uris\=at://did:plc:ffffffffffffffffffffffff/app.bsky.feed.post/abcd | jq
# {
#   "posts": [
#         ....
#         "text": "And this on-the-ground report from Tanzania, suggesting a positive impact on local manufacturing output..."
#          ...
#   ]
# }

```
