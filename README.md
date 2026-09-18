# EcommerceRAG

E-commerce RAG development foundation using Go, Python, Vue, PostgreSQL and Redis.

This repository contains code, schemas and reusable deployment tools. Development documents and environment configuration are maintained privately outside version control.

Run `bash scripts/install-git-hooks.sh` after cloning, then `bash scripts/check.sh` in a configured development environment. The local hooks reject private documents, environment values and common credential patterns; each contributor must enable them.

Deployment requires a protected `/etc/ecommerce-rag/infra.env`, based on `scripts/infra.env.example`. Missing or invalid settings stop deployment. Supply real credentials separately outside this repository.
