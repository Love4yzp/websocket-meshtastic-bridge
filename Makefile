IMAGE_NAME=ws-meshtastic
VERSION=v0.7

build:
	docker buildx build --platform linux/arm64 -f ./Dockerfile . -t $(IMAGE_NAME):$(VERSION)

tag:
	docker tag $(IMAGE_NAME):$(VERSION) $(IMAGE_NAME):latest

run:
	docker run -d -p 5800:5800 --device=/dev/ttyACM0:/dev/ttyACM0 --restart=always --name $(IMAGE_NAME) $(IMAGE_NAME):latest

down:
	docker stop $(IMAGE_NAME) && docker rm $(IMAGE_NAME)

logs:
	docker logs -f $(IMAGE_NAME)

shell:
	docker exec -it $(IMAGE_NAME) /bin/sh

all: build tag
