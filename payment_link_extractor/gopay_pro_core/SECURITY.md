# Security

## Data handling

- Access tokens and proxy credentials are kept in process memory only.
- The application does not create a database, configuration file, account file, or request log.
- Public job responses omit access tokens, proxy values, ChatGPT account IDs, and internal monitor IDs.
- The default listener is `127.0.0.1`. Do not expose the process directly to the Internet.

## Self-hosting

If remote access is required, place the application behind an authenticated reverse proxy, enable HTTPS, restrict source IPs, and use a dedicated low-privilege operating-system account. Never commit `.env` files, access tokens, or proxy lists.

## Reporting

Do not include live access tokens, proxy credentials, payment links, QR payloads, or callback values in a public issue. Reproduce security reports with synthetic values.
