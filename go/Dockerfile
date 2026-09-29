FROM golang:1.27.1-bookworm AS build
WORKDIR /src
COPY go.mod ./
COPY go.sum ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -p 2 -o /gateway ./cmd/server
FROM debian:bookworm-slim
COPY --from=build /gateway /gateway
USER 65532:65532
ENV HTTP_ADDR=0.0.0.0:8080
ENV APP_ENV=development
EXPOSE 8080
ENTRYPOINT ["/gateway"]
