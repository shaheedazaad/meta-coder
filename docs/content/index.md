# Getting started

MetaCoder helps you code meta-analysis studies with an LLM and review the evidence behind each value. Start with a clear effect definition, a coding manual, and a sheet describing the effects you want from each PDF.

[![The MetaCoder Projects page with a demonstration project](assets/screenshots/home.png)](assets/screenshots/home.png)

## Install or update

The installer sets up MetaCoder and everything it needs on your computer. You do not need to install programming tools separately.

**macOS / Linux** — open **Terminal**, copy and paste this command, then press **Enter**:

```bash
curl -fsSL https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.sh | bash
```

**Windows** — open **PowerShell** from the Start menu, copy and paste this command, then press **Enter**:

```powershell
irm https://github.com/shaheedazaad/meta-coder/releases/latest/download/install.ps1 | iex
```

Wait for the installation to finish and follow any instructions it displays.

## Open MetaCoder

Open a new Terminal or PowerShell window, type the following, and press **Enter**:

```text
meta-coder
```

MetaCoder runs on your computer and opens in your web browser. Keep the Terminal or PowerShell window open while you work. To stop MetaCoder, return to that window and press **Ctrl+C**.

If your computer does not recognise `meta-coder`, check the final installation message for the setup step needed to make the command available. You can ask your institution's IT support to help with that step.

Use **Documentation** in the app header to open this guide. Each project section also links to the relevant page. The guide is included with the app on your computer, so you can read it without an internet connection.

## Update MetaCoder

When an update is available, MetaCoder displays a notification. Stop the app, repeat the installation command above for your operating system, and open MetaCoder again. Your projects, preferences, and saved API keys are kept. Export a project ZIP first if you want a backup.

MetaCoder checks for updates when you open a page, at most once every six hours. Checking for updates does not send your research documents or API keys. If you are offline, you can continue working with the installed version; AI requests still need a connection to your chosen provider.

## Your first project

1. Enter a name on **Projects** and choose **Create project**.
2. [Connect a provider](settings.md) in global **Settings**.
3. [Define your analysis](analysis.md): state exactly which comparison counts as an effect.
4. [Build your coding manual](manual.md): define the fields and categories to extract.
5. [Add a coding sheet](coding-sheet.md): identify each effect with a unique row ID and locator.
6. [Upload source PDFs](sources.md), then [review PDF matches](matching.md).
7. [Run extraction](run.md) with your selected provider and model.
8. [Review results and export](results.md), checking coded values against their supporting evidence.
9. [Validate the coding](validation.md) against human coding on a sample of studies.

> Screenshots throughout this guide show a fictional demonstration project. They do not represent research findings or a live provider run.

## Where your work lives

Projects and non-secret preferences are stored on your computer. API keys are stored separately in your computer's built-in secure storage for passwords and other credentials. AI actions send the relevant documents or extracted text to the provider you choose; storing projects on your computer does not make those requests offline. See [Privacy and data sharing](privacy.md) for what each provider receives and how it may use it.

Use [Project settings](projects.md) to export a ZIP backup. The light, dark, or system theme preference is shared between the app and its bundled guide.
