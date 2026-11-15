# Subscribing to the curated marketplace

The catalogue is published on three channels. All three carry the same
libraries; they differ in what has been proven about the version each one
names. The URLs are in the [README](../README.md#subscribing).

`stable` is the default. A fresh Haywire install subscribes to it alongside the
framework's own feed at `going-haywire.github.io/haywire/marketplace.toml`.

## Why the channels exist

Haywire libraries install into **one shared Python environment**, one at a
time. Each install is a separate dependency resolution, so installing library B
can quietly upgrade something library A needed at an older version. A then
fails the next time you load it, and nothing in the studio can detect that.

The three channels differ in how much has been proven about the versions they
name:

| channel | asserts | updated |
| --- | --- | --- |
| **edge** | this library is in the catalogue, and this name refers to this PyPI project. Nothing about the version. | nightly |
| **latest** | everything _edge_ asserts, plus: this exact version installs and loads on its own. Pinned, so everyone on a given catalogue release gets the same set. | each catalogue release |
| **stable** | everything _latest_ asserts, plus: **this whole set has a single common dependency solution** and every library in it loads. May be a smaller set than `latest`: a library that cannot be fitted alongside the others is left out. | each Haywire release |

How `stable` is chosen: [how-stable-works.md](how-stable-works.md).

## Changing channel

There is no channel switcher in the studio. Open
`~/.haywire/db/haybale_marketplace/marketplace.toml` and change the `url` of
the `[[markets]]` entry. The file is created with the other channel URLs
already written in as comments.

The file is only created when it does not exist. An installation that predates
the curated catalogue (before 2026-08-29) has no curated subscription at all;
add one with **Add Source**.

**Do not subscribe to two channels at once.** They offer the same library
names at different versions, and the studio will treat that as a conflict on
every refresh.

## Archives

Every catalogue release of `stable` and `latest` stays published permanently,
so an installation that must not move can pin to one:
[archives](https://going-haywire.github.io/marketplace/archives.html). Each
entry lists its tag, date, the `haywire-core` version it was solved against,
and how many libraries it holds.
