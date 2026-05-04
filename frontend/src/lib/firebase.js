/**
 * Firebase client initialisation + sign-in helpers.
 *
 * The backend keeps MongoDB Atlas as the source of truth for users,
 * orgs, conversations etc. Firebase is only the credential layer:
 * - Frontend gets an ID token from Firebase after Google or
 *   email/password sign-in.
 * - That ID token is attached to every API request as
 *   `Authorization: Bearer <idToken>` (axios interceptor in lib/api.js).
 * - Backend verifies it via Google's public JWKS — no Firebase Admin
 *   SDK / service account JSON needed.
 *
 * Firebase config keys come from REACT_APP_FIREBASE_* in
 * frontend/.env. If any are missing we bail out early so the app keeps
 * running on legacy JWT auth without throwing.
 */
import { initializeApp, getApps, getApp } from "firebase/app";
import {
  getAuth,
  setPersistence,
  browserLocalPersistence,
  GoogleAuthProvider,
  signInWithPopup,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  sendPasswordResetEmail,
  signOut as fbSignOut,
  onIdTokenChanged,
  updateProfile,
} from "firebase/auth";

const firebaseConfig = {
  apiKey: process.env.REACT_APP_FIREBASE_API_KEY,
  authDomain: process.env.REACT_APP_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.REACT_APP_FIREBASE_PROJECT_ID,
  storageBucket: process.env.REACT_APP_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: process.env.REACT_APP_FIREBASE_MESSAGING_SENDER_ID,
  appId: process.env.REACT_APP_FIREBASE_APP_ID,
};

export const FIREBASE_ENABLED = Boolean(
  firebaseConfig.apiKey && firebaseConfig.authDomain && firebaseConfig.projectId,
);

let _app = null;
let _auth = null;
let _googleProvider = null;

/** Lazy-initialise the Firebase app + Auth instance.  Safe to call
 *  many times — guarded by `getApps().length` so React's StrictMode
 *  double-mount doesn't trip "Firebase already initialised". */
export function getFirebaseAuth() {
  if (!FIREBASE_ENABLED) return null;
  if (_auth) return _auth;
  _app = getApps().length ? getApp() : initializeApp(firebaseConfig);
  _auth = getAuth(_app);
  // Persist sessions across tabs / page reloads (default in browsers,
  // but explicit here so it survives a Safari Private hop).
  setPersistence(_auth, browserLocalPersistence).catch(() => { /* best-effort */ });
  return _auth;
}

function _provider() {
  if (_googleProvider) return _googleProvider;
  _googleProvider = new GoogleAuthProvider();
  // Force the account chooser every time so users on a shared
  // browser don't silently land in the previous account.
  _googleProvider.setCustomParameters({ prompt: "select_account" });
  return _googleProvider;
}

/** Sign in with Google. Resolves to `{ idToken, user }` on success,
 *  throws with a friendly message on failure. */
export async function signInWithGoogle() {
  const auth = getFirebaseAuth();
  if (!auth) throw new Error("Firebase is not configured");
  const result = await signInWithPopup(auth, _provider());
  const idToken = await result.user.getIdToken();
  return { idToken, user: result.user };
}

/** Sign in with email / password. */
export async function signInWithEmail(email, password) {
  const auth = getFirebaseAuth();
  if (!auth) throw new Error("Firebase is not configured");
  const result = await signInWithEmailAndPassword(auth, email, password);
  const idToken = await result.user.getIdToken();
  return { idToken, user: result.user };
}

/** Register a new email/password user. Optionally sets the display
 *  name on the Firebase profile so the backend gets it on first
 *  upsert. */
export async function registerWithEmail({ email, password, name }) {
  const auth = getFirebaseAuth();
  if (!auth) throw new Error("Firebase is not configured");
  const result = await createUserWithEmailAndPassword(auth, email, password);
  if (name && name.trim()) {
    try { await updateProfile(result.user, { displayName: name.trim() }); }
    catch (_) { /* non-fatal */ }
  }
  const idToken = await result.user.getIdToken(true); // force refresh so the new displayName ends up in the token
  return { idToken, user: result.user };
}

/** Send a password-reset email. */
export async function resetPassword(email) {
  const auth = getFirebaseAuth();
  if (!auth) throw new Error("Firebase is not configured");
  await sendPasswordResetEmail(auth, email);
}

/** Sign out of Firebase. */
export async function firebaseSignOut() {
  const auth = getFirebaseAuth();
  if (!auth) return;
  try { await fbSignOut(auth); } catch (_) { /* idempotent */ }
}

/** Get a fresh ID token for the currently signed-in Firebase user.
 *  Returns "" if no Firebase session is active. */
export async function getCurrentIdToken({ forceRefresh = false } = {}) {
  const auth = getFirebaseAuth();
  if (!auth || !auth.currentUser) return "";
  try {
    return await auth.currentUser.getIdToken(forceRefresh);
  } catch (_) {
    return "";
  }
}

/** Subscribe to ID-token changes (login / logout / refresh). The
 *  callback receives the latest user (or null if signed out). */
export function onAuthChange(cb) {
  const auth = getFirebaseAuth();
  if (!auth) {
    cb(null);
    return () => {};
  }
  return onIdTokenChanged(auth, cb);
}

/** Map Firebase auth/* error codes to short, friendly UI strings. */
export function friendlyAuthError(err) {
  const code = err?.code || "";
  switch (code) {
    case "auth/popup-closed-by-user":
    case "auth/cancelled-popup-request":
      return "Sign-in cancelled.";
    case "auth/invalid-email":
      return "That email address looks invalid.";
    case "auth/user-not-found":
    case "auth/invalid-credential":
    case "auth/wrong-password":
      return "Wrong email or password.";
    case "auth/email-already-in-use":
      return "An account with that email already exists — try signing in instead.";
    case "auth/weak-password":
      return "Password is too weak — use at least 6 characters.";
    case "auth/network-request-failed":
      return "Network error — check your connection and try again.";
    case "auth/too-many-requests":
      return "Too many attempts — wait a minute and try again.";
    case "auth/account-exists-with-different-credential":
      return "An account exists with this email under a different sign-in method. Sign in that way first, then link Google.";
    case "auth/popup-blocked":
      return "The browser blocked the sign-in popup. Allow popups for this site and try again.";
    default:
      return err?.message || "Sign-in failed.";
  }
}
