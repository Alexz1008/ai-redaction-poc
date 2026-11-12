import { provideHttpClient } from '@angular/common/http';
import {
  ApplicationConfig,
  inject,
  provideAppInitializer,
  provideBrowserGlobalErrorListeners,
} from '@angular/core';
import { provideRouter } from '@angular/router';
import { ApiConfig } from './api-config';
import { Auth } from './auth';
import { routes } from './app.routes';

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideHttpClient(),
    provideAppInitializer(() => {
      const config = inject(ApiConfig);
      const auth = inject(Auth);
      return config.load().then(() => auth.initialize());
    }),
    provideRouter(routes),
  ],
};
