#!/bin/sh
set -eu

MONGO_APP_PASSWORD="$(cat "$MONGO_APP_PASSWORD_FILE")"
export MONGO_APP_PASSWORD

mongosh --quiet "$MONGO_APP_DATABASE" --eval '
db.createUser({
  user: process.env.MONGO_APP_USERNAME,
  pwd: process.env.MONGO_APP_PASSWORD,
  roles: [{role: "readWrite", db: process.env.MONGO_APP_DATABASE}]
})
'

unset MONGO_APP_PASSWORD
