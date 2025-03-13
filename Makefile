build:
	docker buildx build --platform linux/arm64  -f ./Dockerfile . -t sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1
push:
	docker push sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1
run:
	docker run -d -p 5800:5800 --name websocket-meshtastic-bridge sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1
down:
	docker stop websocket-meshtastic-bridge && docker rm websocket-meshtastic-bridge

all: build push
