import { HttpHeaders } from '@angular/common/http';
import { inject, Injectable, signal } from '@angular/core';
import {
  AccountInfo,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from '@azure/msal-browser';
import { ApiConfig } from './api-config';

@Injectable({ providedIn: 'root' })
export class Auth {
  private readonly config = inject(ApiConfig);
  private client: PublicClientApplication | undefined;
  readonly account = signal<AccountInfo | null>(null);
  readonly configured = signal(false);

  async initialize(): Promise<void> {
    const { tenantId, clientId, apiScope } = this.config.auth;
    if (!tenantId || !clientId || !apiScope) {
      return;
    }
    this.client = new PublicClientApplication({
      auth: {
        clientId,
        authority: `https://login.microsoftonline.com/${tenantId}`,
        redirectUri: window.location.origin,
        postLogoutRedirectUri: window.location.origin,
      },
      cache: { cacheLocation: 'sessionStorage' },
    });
    await this.client.initialize();
    const result = await this.client.handleRedirectPromise();
    const account = result?.account ?? this.client.getAllAccounts()[0] ?? null;
    this.client.setActiveAccount(account);
    this.account.set(account);
    this.configured.set(true);
  }

  async signIn(): Promise<void> {
    if (!this.client) {
      throw new Error('Configure Entra sign-in in config.json first.');
    }
    await this.client.loginRedirect({ scopes: [this.config.auth.apiScope] });
  }

  async signOut(): Promise<void> {
    if (!this.client) {
      throw new Error('Sign-in is not configured.');
    }
    await this.client.logoutRedirect({ account: this.account() });
  }

  async headers(): Promise<HttpHeaders> {
    const account = this.account();
    if (!this.client || !account) {
      throw new Error('Sign in before uploading or downloading documents.');
    }
    const request = { scopes: [this.config.auth.apiScope], account };
    try {
      const result = await this.client.acquireTokenSilent(request);
      return new HttpHeaders({ Authorization: `Bearer ${result.accessToken}` });
    } catch (error: unknown) {
      if (error instanceof InteractionRequiredAuthError) {
        await this.client.acquireTokenRedirect(request);
        throw new Error('Sign-in needs to be renewed. Please sign in again.');
      }
      throw error;
    }
  }
}
