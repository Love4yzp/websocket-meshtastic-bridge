build:
	docker buildx build --platform linux/arm64  -f ./Dockerfile . -t sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.6
push:
	docker push sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:latest
run:
	docker run -d -p 5800:5800 --device=/dev/ttyACM0:/dev/ttyACM0 --restart=always --name websocket-meshtastic-bridge sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:latest
down:
	docker stop websocket-meshtastic-bridge && docker rm websocket-meshtastic-bridge
logs:
	docker logs -f websocket-meshtastic-bridge
shell:
	docker exec -it websocket-meshtastic-bridge /bin/sh
tag:
	docker tag sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:v0.6 sensecraft-missionpack.seeed.cn/suzhou/websocket-meshtastic-bridge:latest

all: build tag push 
