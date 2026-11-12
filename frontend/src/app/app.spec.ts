import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { ApiConfig } from './api-config';
import { App } from './app';

describe('App', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [App],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('shows a successful connection using the configured backend URL', () => {
    TestBed.inject(ApiConfig).apiBaseUrl = 'https://backend.example/api';
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();

    http.expectOne('https://backend.example/api/health').flush({
      status: 'ok',
      service: 'ai-redaction-api',
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="status"]').textContent).toContain(
      'Backend connected: ai-redaction-api',
    );
    expect(fixture.nativeElement.querySelector('button').disabled).toBe(false);
  });

  it('shows connection failures and permits a retry', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const fixture = TestBed.createComponent(App);
    fixture.detectChanges();
    http.expectOne('/api/health').flush('Unavailable', {
      status: 503,
      statusText: 'Service Unavailable',
    });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="status"]').textContent).toContain(
      'Cannot reach the backend',
    );
    fixture.nativeElement.querySelector('button').click();
    http.expectOne('/api/health').flush({ status: 'ok', service: 'ai-redaction-api' });
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[role="status"]').textContent).toContain(
      'Backend connected',
    );
    vi.restoreAllMocks();
  });
});
