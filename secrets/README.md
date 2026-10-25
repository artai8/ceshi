# Runtime secrets

Docker Compose expects the following files in this directory:

- <code>mongo_root_password</code>
- <code>mongo_app_password</code>
- <code>mongo_uri</code>
- <code>bot_token</code>
- <code>api_hash</code>
- <code>session_encryption_key</code>

Each file contains only the secret value, with no variable name or quotes.

The Mongo URI must use the username from <code>MONGO_APP_USERNAME</code> and
the password stored in <code>mongo_app_password</code>. For the bundled Mongo
service its shape is:

~~~text
mongodb://nb_app:URL_ENCODED_PASSWORD@mongo:27017/nb-multitenant?authSource=nb-multitenant
~~~

Generate the Fernet session key with:

~~~bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
~~~

These files are ignored by Git and by the Docker build context. Set directory
permissions to 700 and file permissions to 600 before starting Compose. Back up
<code>session_encryption_key</code> in a separate secret manager; losing it
makes existing Telegram sessions unreadable.
