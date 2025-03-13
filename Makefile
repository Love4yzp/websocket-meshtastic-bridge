build:
	docker buildx build --platform linux/arm64  -f ./Dockerfile . -t sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1
push:
	docker push sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1
run:
	docker run -d -p 8000:8000 --name mission-knn sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.1

all: build push
