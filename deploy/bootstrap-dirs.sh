#!/bin/bash
# Bootstrap script to ensure required directories exist
set -e

mkdir -p "$HOME/work-queue"
mkdir -p "$HOME/work-done"
mkdir -p "$HOME/work-failed"
mkdir -p "$HOME/work-inbox"
mkdir -p "$HOME/work-outbox"
mkdir -p "$HOME/work-archive"

echo "Directories OK"
