import { bootstrapApplication } from '@angular/platform-browser';
import { appConfig } from './app/app.config';
import { App } from './app/app';

bootstrapApplication(App, appConfig).catch((err: unknown) => {
  console.error('Application startup failed.', err);
  const root = document.querySelector('app-root');
  if (root) {
    root.textContent = 'Application startup failed. Check config.json and the browser console.';
  }
});
