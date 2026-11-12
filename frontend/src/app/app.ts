import { HttpClient } from '@angular/common/http';
import { Component, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ApiConfig } from './api-config';

@Component({
  selector: 'app-root',
  styleUrl: './app.scss',
  templateUrl: './app.html',
})
export class App implements OnInit {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ApiConfig);
  private readonly destroyRef = inject(DestroyRef);
  protected readonly status = signal('Checking backend...');
  protected readonly checking = signal(false);

  ngOnInit(): void {
    this.checkHealth();
  }

  protected checkHealth(): void {
    this.checking.set(true);
    this.status.set('Checking backend...');
    this.http
      .get<{ status: string; service: string }>(`${this.config.apiBaseUrl}/health`)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (health) => {
          this.status.set(
            health.status === 'ok'
              ? `Backend connected: ${health.service}`
              : 'Backend returned an unexpected health status.',
          );
          this.checking.set(false);
        },
        error: (error: unknown) => {
          console.error('Backend health check failed.', error);
          this.status.set('Cannot reach the backend. Check the API URL and backend logs.');
          this.checking.set(false);
        },
      });
  }
}
