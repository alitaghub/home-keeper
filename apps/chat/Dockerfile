# syntax=docker/dockerfile:1
# LangChain Agent Chat UI, unmodified, built from a pinned upstream commit.
# Upstream publishes no tags or image. Bump the commit to update it.
FROM node:24.20.0-slim AS build
ARG CHAT_UI_COMMIT=675be8e530d6ecc78ad44f698c5a64d8b0c3c2cc
# Next.js inlines NEXT_PUBLIC_* values into the browser bundle at build time.
# The browser calls Aegra directly at this URL, so it must be the published one.
# Upstream's /api passthrough stays unused: LANGGRAPH_API_URL is never set.
ARG NEXT_PUBLIC_API_URL=http://localhost:2026
ARG NEXT_PUBLIC_ASSISTANT_ID=agent
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_ASSISTANT_ID=$NEXT_PUBLIC_ASSISTANT_ID \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
ADD https://github.com/langchain-ai/agent-chat-ui.git#${CHAT_UI_COMMIT} .
# corepack picks the pnpm version from upstream's packageManager field.
RUN corepack enable && pnpm install --frozen-lockfile && pnpm build

FROM node:24.20.0-slim
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
# `next start` needs next itself, which upstream lists as a dev dependency.
COPY --from=build --chown=root:root /app ./
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
