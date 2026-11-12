import { HttpHeaders, provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { AccountInfo } from '@azure/msal-browser';
import { ApiConfig } from './api-config';
import { App } from './app';
import { Auth } from './auth';

describe('PDF redaction workflow', () => {
  let http: HttpTestingController;
  let fixture: ComponentFixture<App>;
  const account: AccountInfo = {
    homeAccountId: 'home',
    localAccountId: 'user',
    tenantId: 'tenant',
    environment: 'login.microsoftonline.com',
    username: 'test@example.com',
  };
  const auth = {
    account: signal<AccountInfo | null>(account),
    configured: signal(true),
    headers: vi.fn(async () => new HttpHeaders({ Authorization: 'Bearer access-token' })),
    signIn: vi.fn(async () => {}),
    signOut: vi.fn(async () => {}),
  };

  beforeEach(() => {
    vi.useFakeTimers();
    auth.account.set(account);
    auth.configured.set(true);
    auth.headers.mockClear();
    TestBed.configureTestingModule({
      imports: [App],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Auth, useValue: auth },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    TestBed.inject(ApiConfig).apiBaseUrl = 'https://backend.example/api';
    fixture = TestBed.createComponent(App);
    fixture.detectChanges();
  });

  afterEach(() => {
    fixture.destroy();
    http.verify();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  function button(text: string): HTMLButtonElement {
    const buttons = Array.from(fixture.nativeElement.querySelectorAll('button'));
    const found = buttons.find(
      (element) => element instanceof HTMLButtonElement && element.textContent?.includes(text),
    );
    if (!(found instanceof HTMLButtonElement)) {
      throw new Error(`Button not found: ${text}`);
    }
    return found;
  }

  function select(file: File): void {
    const input = fixture.nativeElement.querySelector('input');
    Object.defineProperty(input, 'files', { configurable: true, value: [file] });
    input.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  }

  it('requires sign-in before offering an upload', () => {
    auth.account.set(null);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('input')).toBeNull();
    expect(button('Sign in with Microsoft')).toBeDefined();
    http.expectNone('https://backend.example/api/documents');
  });

  it('uploads with an access token, polls completion, and downloads the redacted PDF', async () => {
    const createObjectURL = vi.fn(() => 'blob:redacted');
    const revokeObjectURL = vi.fn();
    vi.stubGlobal(
      'URL',
      class extends URL {
        static override createObjectURL = createObjectURL;
        static override revokeObjectURL = revokeObjectURL;
      },
    );
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    select(new File(['%PDF-1.7 synthetic'], 'sample.pdf', { type: 'application/pdf' }));
    button('Upload and redact').click();
    await Promise.resolve();
    const upload = http.expectOne('https://backend.example/api/documents');
    expect(upload.request.headers.get('Authorization')).toBe('Bearer access-token');
    expect(upload.request.body instanceof FormData).toBe(true);
    expect(upload.request.body.get('file').name).toBe('sample.pdf');
    upload.flush({ id: 'document-1', status: 'running', message: null, warning_count: 0 });

    await vi.advanceTimersByTimeAsync(0);
    http.expectOne('https://backend.example/api/documents/document-1').flush({
      id: 'document-1',
      status: 'running',
      message: null,
      warning_count: 0,
    });
    await vi.advanceTimersByTimeAsync(2000);
    const poll = http.expectOne('https://backend.example/api/documents/document-1');
    expect(poll.request.headers.get('Authorization')).toBe('Bearer access-token');
    poll.flush({ id: 'document-1', status: 'succeeded', message: null, warning_count: 1 });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('1 processing warning(s)');
    button('Download redacted PDF').click();
    await Promise.resolve();

    const download = http.expectOne('https://backend.example/api/documents/document-1/download');
    expect(download.request.responseType).toBe('blob');
    expect(download.request.headers.get('Authorization')).toBe('Bearer access-token');
    download.flush(new Blob(['%PDF-redacted'], { type: 'application/pdf' }));
    await Promise.resolve();
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2000);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:redacted');
    http.expectNone('https://backend.example/api/documents/document-1');
  });

  it('rejects non-PDF files and files beyond the 10 MB limit', () => {
    select(new File(['text'], 'sample.txt'));
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain('PDF');
    expect(button('Upload and redact').disabled).toBe(true);
    const large = new File(['%PDF-'], 'large.pdf');
    Object.defineProperty(large, 'size', { value: 10_000_001 });
    select(large);
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain('10 MB');
    expect(button('Upload and redact').disabled).toBe(true);
  });

  it('shows upload errors without offering an unredacted download', async () => {
    select(new File(['%PDF-'], 'sample.pdf'));
    button('Upload and redact').click();
    await Promise.resolve();
    http
      .expectOne('https://backend.example/api/documents')
      .flush(
        { detail: 'Backend Azure settings are not configured.' },
        { status: 503, statusText: 'Service Unavailable' },
      );
    await Promise.resolve();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[role="alert"]').textContent).toContain(
      'Backend Azure settings are not configured.',
    );
    expect(fixture.nativeElement.textContent).not.toContain('Download redacted PDF');
    expect(button('Upload and redact').disabled).toBe(false);
  });
});
