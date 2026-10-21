// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using System.IO.Compression;
using System.Xml;
using System.Xml.Linq;

namespace KnownTestFailures;

public sealed record PreparedArchives(int SchemaVersion, string Project, string ExecutedSha, ProductionModule[] Modules);

public static class ArchivedExecution
{
    public static async Task PrepareAsync(string[] args)
    {
        Format.Require(args.Length == 9 && args[1] == "--repo" && args[3] == "--project" &&
            args[5] == "--output" && args[7] == "--manifest",
            "prepare-archives requires --repo DIR --project PATH --output DIR --manifest FILE.");
        var repo = Path.GetFullPath(args[2]);
        var project = ProductionRunner.NormalizeProjectPath(repo, args[4]);
        var root = Path.GetFullPath(args[6]);
        Format.Require(!Directory.Exists(root) && !File.Exists(args[8]), "Archive output and manifest must be new.");
        var receipts = args[8] + ".receipts";
        Directory.CreateDirectory(receipts);
        var modules = await ProductionRunner.ResolveAsync(repo, project, receipts,
            Environment.GetEnvironmentVariable("KTF_BASE_ARGS") ?? "",
            Environment.GetEnvironmentVariable("KTF_EXTRA_ARGS") ?? "",
            Environment.GetEnvironmentVariable("KTF_VERSION_ARGS") ?? "", true).ConfigureAwait(false);
        var prepared = Extract(modules, root);
        var sha = await GitRegistry.HeadAsync(repo).ConfigureAwait(false);
        File.WriteAllText(args[8], Format.Write(new PreparedArchives(1, project, sha, prepared)));
    }

    public static ProductionModule[] Extract(ProductionModule[] modules, string root)
    {
        Format.Require(modules.Length > 0 && !Directory.Exists(root), "Archive destination must be new.");
        Directory.CreateDirectory(root);
        var prepared = new List<ProductionModule>();
        foreach (var module in modules)
        {
            Format.Require(module.ArchivePath is not null && module.ArchiveAssembly is not null,
                "Missing evaluated archive output.");
            using var zip = ZipFile.OpenRead(module.ArchivePath);
            var manifests = zip.Entries.Where(e => e.FullName == "aspire-known-test-failures.xml").ToArray();
            Format.Require(manifests.Length == 1, "Archive must contain exactly one build-authored module manifest.");
            using var stream = manifests[0].Open();
            using var reader = XmlReader.Create(stream, new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null });
            var manifest = XDocument.Load(reader).Root!;
            Format.Require(manifest.Name == "module" && !manifest.Elements().Any() &&
                manifest.Attributes().Select(a => a.Name.ToString()).Order(StringComparer.Ordinal)
                    .SequenceEqual(new[] { "assembly", "framework", "project", "schemaVersion", "supportedAdapter" }),
                "Unsupported archive manifest shape.");
            Format.Require((string?)manifest.Attribute("schemaVersion") == "1" &&
                (string?)manifest.Attribute("project") == module.Project &&
                (string?)manifest.Attribute("framework") == module.Framework &&
                (string?)manifest.Attribute("assembly") == module.ArchiveAssembly &&
                (string?)manifest.Attribute("supportedAdapter") == module.SupportedAdapter.ToString().ToLowerInvariant(),
                "Archive manifest differs from the selected evaluated module.");
            var destination = modules.Length == 1 ? root : Path.Combine(root, module.Id);
            Directory.CreateDirectory(destination);
            var paths = new HashSet<string>(OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
            foreach (var entry in zip.Entries)
            {
                var relative = entry.FullName.TrimEnd('/');
                Format.Path(relative, "archive entry");
                Format.Require(paths.Add(relative), "Duplicate archive entry.");
                var type = (entry.ExternalAttributes >> 16) & 0xf000;
                Format.Require(type is 0 or 0x8000 or 0x4000, "Unsupported archive file type.");
                var path = Path.Combine(destination, relative);
                if (entry.FullName.EndsWith('/'))
                {
                    Directory.CreateDirectory(path);
                    continue;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                entry.ExtractToFile(path, false);
                if (!OperatingSystem.IsWindows())
                {
                    // ZipDirectory records Unix permission bits. Keep executable fixtures
                    // usable just as the baseline unzip does, without accepting symlinks.
                    var mode = (entry.ExternalAttributes >> 16) & 0x1ff;
                    if (mode != 0)
                    {
                        File.SetUnixFileMode(path, (UnixFileMode)mode);
                    }
                }
            }
            Format.Path(module.ArchiveAssembly, "archive assembly");
            var assembly = Path.Combine(destination, module.ArchiveAssembly);
            Format.Require(File.Exists(assembly), "Selected archived assembly is missing.");
            prepared.Add(module with
            {
                Assembly = assembly, PrefixArguments = [assembly], WorkingDirectory = destination,
                Environment = new(StringComparer.Ordinal)
            });
        }
        return [.. prepared];
    }
}
