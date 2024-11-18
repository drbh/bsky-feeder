from typing import Optional, Dict
import threading
import signal
import sys
from collections import defaultdict
import logging
from datetime import datetime, timezone

# server and db
from fastapi import FastAPI, HTTPException
from peewee import SqliteDatabase, Model, CharField, TextField, DateTimeField

# bsky related
from atproto import (
    AtUri,
    CAR,
    FirehoseSubscribeReposClient,
    models,
    parse_subscribe_repos_message,
    firehose_models,
)

# LLM related
from mlx_lm import load, generate


# init db
db = SqliteDatabase("feed.db")


class Post(Model):
    uri = CharField(unique=True)
    cid = CharField()
    reply_parent = TextField(null=True)
    reply_root = TextField(null=True)
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))
    author = CharField()
    text = TextField()

    class Meta:
        database = db


db.create_tables([Post])

# server setup
app = FastAPI(
    title="AT Protocol Feed Generator",
    description="A feed generator for Bluesky that uses a local LLM model to determine if a post meets a certain criteria",
    version="1.0.0",
)

# setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# load LLM model
model_path = "mlx-community/Qwen2-7B-Instruct-4bit"
model, tokenizer = load(model_path)

QUERY = "Does this post contain information about science? (yes|no). Be very strict and only include posts that are about science."


class FeedGenerator:
    def __init__(self, service_did: str, hostname: str):
        self.service_did = service_did
        self.hostname = hostname
        self.stop_event = threading.Event()
        self._connect_retries = 0
        self.MAX_RETRIES = 5

    def process_post(self, post_data: Dict) -> Optional[Dict]:
        try:
            record = post_data["record"]
            text = record.text.lower()

            # log new posts as we process them
            logger.info(
                f"New post from {post_data['author']}\n"
                f"Text: {record.text[:100]}{'...' if len(record.text) > 100 else ''}"
            )

            # write a prompt to determine if the post is about science
            prompt = f"{QUERY}\n\n{text}"

            # build convo and generate response
            conversation = []
            conversation.append({"role": "user", "content": prompt})
            prompt = tokenizer.apply_chat_template(
                conversation=conversation, tokenize=False, add_generation_prompt=True
            )
            generation_args = {
                "temp": 0.0,
                "max_tokens": 4,
            }
            response = generate(
                model,
                tokenizer,
                prompt=prompt,
                verbose=False,
                **generation_args,
            ).strip()
            response = response.lower()
            logger.info(f"Response: {response}")  # show response in logs

            should_include = "yes" in response
            if should_include:
                return {
                    "uri": post_data["uri"],
                    "cid": post_data["cid"],
                    "reply_parent": (
                        record.reply.parent.uri
                        if getattr(record, "reply", None)
                        else None
                    ),
                    "reply_root": (
                        record.reply.root.uri
                        if getattr(record, "reply", None)
                        else None
                    ),
                    "author": post_data["author"],
                    "text": record.text,
                }

            return None
        except Exception as e:
            logger.error(f"Error processing post: {e}", exc_info=True)
            return None

    def handle_operations(self, ops: defaultdict) -> None:
        try:
            # process new posts
            posts_to_create = []
            for post_data in ops["posts"]["created"]:
                if processed := self.process_post(post_data):
                    posts_to_create.append(processed)

            # handle deletions
            if deleted := ops["posts"]["deleted"]:
                uris = [post["uri"] for post in deleted]
                Post.delete().where(Post.uri.in_(uris)).execute()
                logger.info(f"Deleted posts: {len(uris)}")

            # save new posts
            if posts_to_create:
                with db.atomic():
                    Post.bulk_create([Post(**post) for post in posts_to_create])
                logger.info(f"Added posts: {len(posts_to_create)}")

        except Exception as e:
            logger.error(f"Error handling operations: {e}", exc_info=True)

    def process_message(
        self,
        client: FirehoseSubscribeReposClient,
        message: firehose_models.MessageFrame,
    ) -> None:
        if self.stop_event.is_set():
            client.stop()
            return

        try:
            commit = parse_subscribe_repos_message(message)
            if (
                not isinstance(commit, models.ComAtprotoSyncSubscribeRepos.Commit)
                or not commit.blocks
            ):
                return

            operations = defaultdict(lambda: defaultdict(list))
            car = CAR.from_bytes(commit.blocks)

            for op in commit.ops:
                if op.action == "update":
                    continue

                uri = AtUri.from_str(f"at://{commit.repo}/{op.path}")

                if op.action == "create" and op.cid:
                    record_data = car.blocks.get(op.cid)
                    if not record_data:
                        continue

                    record = models.get_or_create(record_data, strict=False)
                    if uri.collection == "app.bsky.feed.post" and hasattr(
                        record, "text"
                    ):  # Check if it's a post record
                        operations["posts"]["created"].append(
                            {
                                "uri": str(uri),
                                "cid": str(op.cid),
                                "author": commit.repo,
                                "record": record,
                            }
                        )
                elif op.action == "delete":
                    operations["posts"]["deleted"].append({"uri": str(uri)})

            self.handle_operations(operations)

        except Exception as e:
            logger.error(f"Error processing message: {e}", exc_info=True)
            self._connect_retries += 1
            if self._connect_retries >= self.MAX_RETRIES:
                logger.error("Max retries reached, stopping client")
                self.stop_event.set()
                client.stop()

    def run(self):
        while not self.stop_event.is_set():
            try:
                client = FirehoseSubscribeReposClient()
                self._connect_retries = (
                    0  # reset retry counter on successful connection
                )
                client.start(lambda msg: self.process_message(client, msg))
            except Exception as e:
                logger.error(f"Connection error: {e}", exc_info=True)
                if not self.stop_event.is_set():
                    threading.Event().wait(5)  # wait alil


HOSTNAME = "feed.example.com"
SERVICE_DID = f"did:web:{HOSTNAME}"
feed_generator = FeedGenerator(SERVICE_DID, HOSTNAME)

# begin firehose subscription for posts in background
threading.Thread(target=feed_generator.run, daemon=True).start()


# graceful shutdown
def shutdown_handler(signum, frame):
    logger.info("Shutting down gracefully...")
    feed_generator.stop_event.set()
    sys.exit(0)


signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)


# API routes
@app.get("/", response_model=Dict[str, str])
async def root():
    return {"name": "ATProto Feed Generator", "version": "1.0.0", "status": "running"}


@app.get("/.well-known/did.json")
async def did_json():
    if not feed_generator.service_did.endswith(feed_generator.hostname):
        raise HTTPException(status_code=404)

    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": feed_generator.service_did,
        "service": [
            {
                "id": "#bsky_fg",
                "type": "BskyFeedGenerator",
                "serviceEndpoint": f"https://{feed_generator.hostname}",
            }
        ],
    }


@app.get("/xrpc/app.bsky.feed.describeFeedGenerator")
async def describe_feed_generator():
    return {
        "did": feed_generator.service_did,
        "feeds": [
            {
                "uri": f"at://{feed_generator.service_did}/app.bsky.feed.generator/drbh-feed",
                "name": "drbhs Feed",
                "description": "A feed of posts that meet the criteria",
            }
        ],
    }


@app.get("/xrpc/app.bsky.feed.getFeedSkeleton")
async def get_feed_skeleton(feed: str, cursor: Optional[str] = None, limit: int = 20):
    if not feed.endswith("drbh-feed"):
        raise HTTPException(status_code=400, detail="Unsupported feed")

    try:
        query = Post.select().order_by(Post.created_at.desc()).limit(limit)
        if cursor:
            query = query.where(Post.uri < cursor)

        posts = list(query.execute())
        return {
            "cursor": posts[-1].uri if posts else None,
            "feed": [{"post": post.uri} for post in posts],
        }
    except Exception as e:
        logger.error(f"Error fetching feed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
