import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';

@Injectable({ providedIn: 'root' })
export class ApiConfig {
  private readonly http = inject(HttpClient);
  apiBaseUrl = '/api';

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
  }
}
