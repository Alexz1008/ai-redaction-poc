# Frontend

Generated with the official Angular CLI using routing, SCSS, and strict mode.
See the root [README](../README.md) for prerequisites, local development, tests,
and runtime API configuration.

- `npm start`: development server on port 4200, with `/api/*` proxied to port 8000.
- `npm test -- --watch=false`: headless unit tests.
- `npm run build`: production assets in `dist/frontend/browser`.

`public/config.json` uses `/api` locally. Set the built artifact's `config.json`
to `{ "apiBaseUrl": "https://<backend-host>/api" }` for a separate deployment.
This configuration is public and must never contain secrets.
