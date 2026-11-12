import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ApiConfig } from './api-config';

describe('ApiConfig', () => {
  let http: HttpTestingController;
  let config: ApiConfig;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    config = TestBed.inject(ApiConfig);
  });

  afterEach(() => http.verify());

  it('loads the deployed API URL and removes a trailing slash', async () => {
    const loaded = config.load();
    http.expectOne('/config.json').flush({ apiBaseUrl: 'https://backend.example/api/' });
    await loaded;
    expect(config.apiBaseUrl).toBe('https://backend.example/api');
  });

  it('rejects invalid configuration instead of silently using the local proxy', async () => {
    const loaded = config.load();
    const rejected = expect(loaded).rejects.toThrow('apiBaseUrl');
    http.expectOne('/config.json').flush({ apiBaseUrl: 42 });
    await rejected;
  });

  it('propagates configuration download failures', async () => {
    const loaded = config.load();
    const rejected = expect(loaded).rejects.toBeDefined();
    http.expectOne('/config.json').flush('Not Found', { status: 404, statusText: 'Not Found' });
    await rejected;
  });
});
