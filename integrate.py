#!/usr/bin/env python3
"""FlarixLauncher integration. Run from the repo root (the build workflow does this).
Adds the Flarix WebView UI + secure JS bridge to the MojoLauncher app module without touching engine code.
Idempotent: safe to run on every build."""
import os, re, sys

def find_app_module(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in ('build', '.git', 'node_modules', '.gradle')]
        for f in fn:
            if f in ('build.gradle', 'build.gradle.kts'):
                t = open(os.path.join(dp, f), encoding='utf-8', errors='ignore').read()
                if 'com.android.application' in t and os.path.isfile(os.path.join(dp, 'src/main/AndroidManifest.xml')):
                    return dp, t
    return None, None

def main():
    root = os.getcwd()
    mod, gradle = find_app_module(root)
    if not mod:
        sys.exit('ERROR: no Android application module found.')
    manifest_path = os.path.join(mod, 'src/main/AndroidManifest.xml')
    manifest = open(manifest_path, encoding='utf-8').read()
    m = re.search(r'namespace\s*=?\s*["\']([\w.]+)["\']', gradle) or re.search(r'package="([\w.]+)"', manifest)
    if not m:
        sys.exit('ERROR: cannot determine application package.')
    pkg = m.group(1)
    jdir = os.path.join(mod, 'src/main/java', *pkg.split('.'), 'flarix')
    os.makedirs(jdir, exist_ok=True)
    for name, src in (('FlarixActivity.java', ACTIVITY), ('FlarixBridge.java', BRIDGE)):
        open(os.path.join(jdir, name), 'w', encoding='utf-8').write(src.replace('__PKG__', pkg))
    adir = os.path.join(mod, 'src/main/assets/flarix')
    os.makedirs(adir, exist_ok=True)
    open(os.path.join(adir, 'index.html'), 'w', encoding='utf-8').write(HTML)
    if 'FlarixActivity' not in manifest:
        act = ('        <activity android:name="%s.flarix.FlarixActivity" android:exported="true"\n'
               '            android:label="Flarix Launcher" android:configChanges="orientation|screenSize|keyboardHidden"\n'
               '            android:theme="@android:style/Theme.DeviceDefault.NoActionBar">\n'
               '            <intent-filter>\n'
               '                <action android:name="android.intent.action.MAIN"/>\n'
               '                <category android:name="android.intent.category.LAUNCHER"/>\n'
               '            </intent-filter>\n'
               '        </activity>\n    ') % pkg
        if '</application>' not in manifest:
            sys.exit('ERROR: manifest has no </application>.')
        manifest = manifest.replace('</application>', act + '</application>', 1)
    if 'android.permission.INTERNET' not in manifest:
        manifest = re.sub(r'(<manifest[^>]*>)', r'\1\n    <uses-permission android:name="android.permission.INTERNET"/>', manifest, count=1)
    open(manifest_path, 'w', encoding='utf-8').write(manifest)
    print('Flarix integrated: module=%s package=%s' % (mod, pkg))

ACTIVITY = r'''package __PKG__.flarix;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.ByteArrayInputStream;

/** Hosts the Flarix HTML UI. Only bundled local assets can load; all other navigation is blocked. */
public class FlarixActivity extends Activity {
    private static final String BASE = "file:///android_asset/flarix/";
    private WebView web;
    private FlarixBridge bridge;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0B1118"));
        setContentView(web);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);          // does not affect file:///android_asset
        s.setAllowContentAccess(false);
        s.setAllowFileAccessFromFileURLs(false);
        s.setAllowUniversalAccessFromFileURLs(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        s.setGeolocationEnabled(false);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                return !r.getUrl().toString().startsWith(BASE);
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView v, WebResourceRequest r) {
                if (!"file".equals(r.getUrl().getScheme())) {
                    return new WebResourceResponse("text/plain", "utf-8", new ByteArrayInputStream(new byte[0]));
                }
                return null;
            }
        });

        bridge = new FlarixBridge(this, web);
        web.addJavascriptInterface(bridge, "FlarixNative");
        web.loadUrl(BASE + "index.html");
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        bridge.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    protected void onDestroy() {
        if (web != null) { web.removeJavascriptInterface("FlarixNative"); web.destroy(); }
        super.onDestroy();
    }
}
'''

BRIDGE = r'''package __PKG__.flarix;

import android.app.Activity;
import android.app.ActivityManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.graphics.BitmapFactory;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.provider.OpenableColumns;
import android.util.Base64;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

/**
 * JavaScript bridge. A single entry point (call) with a fixed whitelist of methods;
 * every argument is validated and file access is confined to the Flarix data directory.
 */
final class FlarixBridge {
    static final int REQ_MOD = 7101;
    static final int REQ_SKIN = 7102;
    private static final Object DEFERRED = new Object();
    private static final String MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json";
    private static final Pattern PLAYER = Pattern.compile("^[A-Za-z0-9_]{3,16}$");
    private static final Pattern VERSION = Pattern.compile("^[A-Za-z0-9._ \\-]{1,40}$");
    private static final Pattern MODFILE = Pattern.compile("^[A-Za-z0-9._+ \\-]{1,120}$");

    private final Activity activity;
    private final WebView web;
    private final SharedPreferences prefs;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newCachedThreadPool();
    private volatile String pendingMod, pendingSkin;

    FlarixBridge(Activity activity, WebView web) {
        this.activity = activity;
        this.web = web;
        this.prefs = activity.getSharedPreferences("flarix", Context.MODE_PRIVATE);
    }

    @JavascriptInterface
    public void call(final String id, final String method, final String args) {
        io.execute(() -> {
            try {
                Object r = dispatch(id, method, new JSONObject(args));
                if (r != DEFERRED) reply(id, true, r);
            } catch (Exception e) {
                reply(id, false, e.getMessage() == null ? e.toString() : e.getMessage());
            }
        });
    }

    private Object dispatch(String id, String method, JSONObject a) throws Exception {
        switch (method) {
            case "getSettings": return settings();
            case "setSettings": {
                int ram = clampRam(a.getInt("ramMb"));
                prefs.edit().putInt("ramMb", ram).apply();
                return settings();
            }
            case "fetchVersions": return fetchVersions(a.optBoolean("snapshots", false));
            case "saveProfile": {
                String name = a.getString("name").trim();
                String version = a.getString("version").trim();
                if (!PLAYER.matcher(name).matches())
                    throw new Exception("Username must be 3-16 letters, numbers or underscores.");
                if (!VERSION.matcher(version).matches()) throw new Exception("Invalid version.");
                String uuid = UUID.nameUUIDFromBytes(("OfflinePlayer:" + name).getBytes(StandardCharsets.UTF_8)).toString();
                prefs.edit().putString("name", name).putString("uuid", uuid).putString("version", version).apply();
                return new JSONObject().put("name", name).put("uuid", uuid).put("version", version);
            }
            case "launch":
                throw new Exception("Game launch is not connected to the MojoLauncher engine yet.");
            case "microsoftLogin":
                throw new Exception("Microsoft sign-in is not connected yet.");
            case "listMods": return listMods();
            case "pickMod": pick(REQ_MOD, "*/*", id); return DEFERRED;
            case "setModEnabled": return setModEnabled(a.getString("name"), a.getBoolean("enabled"));
            case "removeMod": {
                File f = modFile(a
