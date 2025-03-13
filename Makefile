build:
	docker buildx build --platform linux/arm64  -f ./Dockerfile . -t sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.2
push:
	docker push sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.2
run:
	docker run -d -p 5800:5800 --device=/dev/ttyACM0:/dev/ttyACM0 --name websocket-meshtastic-bridge sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.2
down:
	docker stop websocket-meshtastic-bridge && docker rm websocket-meshtastic-bridge
logs:
	docker logs -f websocket-meshtastic-bridge
shell:
	docker exec -it websocket-meshtastic-bridge /bin/sh

all: build push
