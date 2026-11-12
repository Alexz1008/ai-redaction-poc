import { TestBed } from '@angular/core/testing';
import {
  AccountInfo,
  AuthenticationResult,
  InteractionRequiredAuthError,
  PublicClientApplication,
} from '@azure/msal-browser';
import { ApiConfig } from './api-config';
import { Auth } from './auth';

describe('Entra authentication', () => {
  const account: AccountInfo = {
    homeAccountId: 'home',
    localAccountId: 'user',
    tenantId: 'tenant',
    environment: 'login.microsoftonline.com',
    username: 'test@example.com',
  };
  let auth: Auth;

  beforeEach(() => {
    vi.spyOn(PublicClientApplication.prototype, 'initialize').mockResolvedValue(undefined);
    vi.spyOn(PublicClientApplication.prototype, 'handleRedirectPromise').mockResolvedValue(null);
    vi.spyOn(PublicClientApplication.prototype, 'getAllAccounts').mockReturnValue([account]);
    vi.spyOn(PublicClientApplication.prototype, 'setActiveAccount').mockImplementation(() => {});
    TestBed.configureTestingModule({
      providers: [
        {
          provide: ApiConfig,
          useValue: {
            auth: {
              tenantId: 'tenant',
              clientId: 'spa',
              apiScope: 'api://api/access_as_user',
            },
          },
        },
      ],
    });
    auth = TestBed.inject(Auth);
  });

  afterEach(() => vi.restoreAllMocks());

  it('uses the delegated access token rather than an ID token for API calls', async () => {
    const result: AuthenticationResult = {
      authority: 'https://login.microsoftonline.com/tenant',
      uniqueId: 'user',
      tenantId: 'tenant',
      scopes: ['api://api/access_as_user'],
      account,
      idToken: 'id-token',
      idTokenClaims: {},
      accessToken: 'api-token',
      fromCache: true,
      expiresOn: new Date(Date.now() + 3600_000),
      tokenType: 'Bearer',
      correlationId: 'correlation',
    };
    const silent = vi
      .spyOn(PublicClientApplication.prototype, 'acquireTokenSilent')
      .mockResolvedValue(result);
    await auth.initialize();
    expect(auth.account()?.localAccountId).toBe('user');
    const headers = await auth.headers();
    expect(headers.get('Authorization')).toBe('Bearer api-token');
    expect(silent).toHaveBeenCalledWith({
      scopes: ['api://api/access_as_user'],
      account,
    });
  });

  it('requests a new sign-in when Entra requires interaction', async () => {
    vi.spyOn(PublicClientApplication.prototype, 'acquireTokenSilent').mockRejectedValue(
      new InteractionRequiredAuthError('login_required'),
    );
    const redirect = vi
      .spyOn(PublicClientApplication.prototype, 'acquireTokenRedirect')
      .mockResolvedValue(undefined);
    await auth.initialize();
    await expect(auth.headers()).rejects.toThrow('Sign-in needs to be renewed');
    expect(redirect).toHaveBeenCalledOnce();
  });

  it('does not silently hide token acquisition failures', async () => {
    vi.spyOn(PublicClientApplication.prototype, 'acquireTokenSilent').mockRejectedValue(
      new Error('Token acquisition failed'),
    );
    const redirect = vi.spyOn(PublicClientApplication.prototype, 'acquireTokenRedirect');
    await auth.initialize();
    await expect(auth.headers()).rejects.toThrow('Token acquisition failed');
    expect(redirect).not.toHaveBeenCalled();
  });
});
