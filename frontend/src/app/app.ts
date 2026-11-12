import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Component, DestroyRef, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { exhaustMap, firstValueFrom, from, Subscription, switchMap, takeWhile, timer } from 'rxjs';
import { ApiConfig } from './api-config';
import { Auth } from './auth';

interface Job {
  id: string;
  status: 'running' | 'succeeded' | 'failed';
  message: string | null;
  warning_count: number;
}

@Component({
  selector: 'app-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App {
  protected readonly auth = inject(Auth);
  private readonly http = inject(HttpClient);
  private readonly config = inject(ApiConfig);
  private readonly destroyRef = inject(DestroyRef);
  private pollSubscription: Subscription | undefined;
  protected readonly file = signal<File | null>(null);
  protected readonly job = signal<Job | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly uploading = signal(false);
  protected readonly polling = signal(false);
  protected readonly downloading = signal(false);

  protected async signIn(): Promise<void> {
    try {
      await this.auth.signIn();
    } catch (error: unknown) {
      this.showError(error, 'Sign-in failed.');
    }
  }

  protected async signOut(): Promise<void> {
    try {
      await this.auth.signOut();
    } catch (error: unknown) {
      this.showError(error, 'Sign-out failed.');
    }
  }

  protected selectFile(event: Event): void {
    const input = event.target;
    if (!(input instanceof HTMLInputElement)) {
      return;
    }
    this.pollSubscription?.unsubscribe();
    this.polling.set(false);
    this.job.set(null);
    this.error.set(null);
    this.file.set(null);
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    if (!file.name.toLowerCase().endsWith('.pdf')) {
      this.error.set('Choose a PDF document.');
    } else if (file.size === 0 || file.size > 10_000_000) {
      this.error.set('Choose a nonempty PDF no larger than 10 MB.');
    } else {
      this.file.set(file);
    }
  }

  protected async upload(): Promise<void> {
    const file = this.file();
    if (!file || this.uploading() || this.polling()) {
      return;
    }
    this.error.set(null);
    this.job.set(null);
    this.uploading.set(true);
    try {
      const headers = await this.auth.headers();
      const body = new FormData();
      body.append('file', file);
      const job = await firstValueFrom(
        this.http
          .post<Job>(`${this.config.apiBaseUrl}/documents`, body, { headers })
          .pipe(takeUntilDestroyed(this.destroyRef)),
      );
      this.job.set(job);
      this.startPolling();
    } catch (error: unknown) {
      this.showError(error, 'Upload failed. Please try again.');
    } finally {
      this.uploading.set(false);
    }
  }

  protected startPolling(): void {
    const job = this.job();
    if (!job || job.status !== 'running' || this.polling()) {
      return;
    }
    this.error.set(null);
    this.polling.set(true);
    this.pollSubscription = timer(0, 2000)
      .pipe(
        exhaustMap(() =>
          from(this.auth.headers()).pipe(
            switchMap((headers) =>
              this.http.get<Job>(`${this.config.apiBaseUrl}/documents/${job.id}`, { headers }),
            ),
          ),
        ),
        takeWhile((result) => result.status === 'running', true),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe({
        next: (result) => this.job.set(result),
        error: (error: unknown) => {
          this.polling.set(false);
          this.showError(error, 'Could not check progress. Use Check status to resume.');
        },
        complete: () => this.polling.set(false),
      });
  }

  protected async download(): Promise<void> {
    const job = this.job();
    if (job?.status !== 'succeeded' || this.downloading()) {
      return;
    }
    this.error.set(null);
    this.downloading.set(true);
    try {
      const headers = await this.auth.headers();
      const pdf = await firstValueFrom(
        this.http
          .get(`${this.config.apiBaseUrl}/documents/${job.id}/download`, {
            headers,
            responseType: 'blob',
          })
          .pipe(takeUntilDestroyed(this.destroyRef)),
      );
      const url = URL.createObjectURL(pdf);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'redacted.pdf';
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error: unknown) {
      this.showError(error, 'Download failed. Please try again.');
    } finally {
      this.downloading.set(false);
    }
  }

  private showError(error: unknown, fallback: string): void {
    if (error instanceof HttpErrorResponse) {
      const body: unknown = error.error;
      this.error.set(
        typeof body === 'object' &&
          body !== null &&
          'detail' in body &&
          typeof body.detail === 'string'
          ? body.detail
          : fallback,
      );
    } else {
      this.error.set(error instanceof Error ? error.message : fallback);
    }
  }
}
