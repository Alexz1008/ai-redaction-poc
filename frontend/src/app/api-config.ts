import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ApiConfig {
  private readonly http = inject(HttpClient);
  apiBaseUrl = '/api';
  auth = { tenantId: '', clientId: '', apiScope: '' };

  async load(): Promise<void> {
    const config = await firstValueFrom(this.http.get<unknown>('/config.json'));
    if (
      typeof config !== 'object' ||
      config === null ||
      !('apiBaseUrl' in config) ||
      typeof config.apiBaseUrl !== 'string'
    ) {
      throw new Error('config.json must define an apiBaseUrl string.');
    }

    const url = config.apiBaseUrl.replace(/\/+$/, '');
    if (url !== '/api') {
      const parsed = new URL(url);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.search || parsed.hash) {
        throw new Error('apiBaseUrl must be /api or an HTTP(S) URL without a query or fragment.');
      }
    }
    this.apiBaseUrl = url;
    if (
      !('auth' in config) ||
      typeof config.auth !== 'object' ||
      config.auth === null ||
      !('tenantId' in config.auth) ||
      typeof config.auth.tenantId !== 'string' ||
      !('clientId' in config.auth) ||
      typeof config.auth.clientId !== 'string' ||
      !('apiScope' in config.auth) ||
      typeof config.auth.apiScope !== 'string'
    ) {
      throw new Error('config.json must define auth.tenantId, auth.clientId and auth.apiScope.');
    }
    this.auth = {
      tenantId: config.auth.tenantId,
      clientId: config.auth.clientId,
      apiScope: config.auth.apiScope,
    };
  }
}
