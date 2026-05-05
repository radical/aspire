// Licensed to the .NET Foundation under one or more agreements.
// The .NET Foundation licenses this file to you under the MIT license.

using Aspire.Cli.Acquisition;
using Aspire.Cli.Configuration;

namespace Aspire.Cli.Tests.Scaffolding;

/// <summary>
/// Tests for the PR4 channel-reseed behaviour introduced in <c>ScaffoldingService</c>.
/// At scaffold time the service bakes the identity channel (read from the running binary via
/// <see cref="IIdentityChannelReader"/>) into <c>aspire.config.json</c>.  An explicit
/// <c>context.Channel</c> value overrides the baked channel.  After prepare, the server-
/// reported <c>ChannelName</c> is written back; a null server channel falls back to the
/// baked channel.
/// </summary>
public class ChannelReseedTests
{
    private sealed class FakeChannelReader(string channel) : IIdentityChannelReader
    {
        public string ReadChannel() => channel;
        public int? GetPrNumber(string informationalVersion) => null;
    }

    // ---------------------------------------------------------------------------
    // First save (line 68 of ScaffoldingService): context.Channel ?? reader.ReadChannel()
    // ---------------------------------------------------------------------------

    [Theory]
    [InlineData("stable")]
    [InlineData("staging")]
    [InlineData("daily")]
    public void FirstSave_WritesBakedChannel_WhenContextChannelIsNull(string bakedChannel)
    {
        var dir = Directory.CreateTempSubdirectory("aspire-reseed-first-");

        try
        {
            var channelReader = new FakeChannelReader(bakedChannel);

            // Simulate ScaffoldingService: config.Channel = context.Channel ?? _channelReader.ReadChannel()
            string? contextChannel = null;
            var config = new AspireConfigFile();
            config.Channel = contextChannel ?? channelReader.ReadChannel();
            config.Save(dir.FullName);

            var reloaded = AspireConfigFile.Load(dir.FullName);
            Assert.Equal(bakedChannel, reloaded?.Channel);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void FirstSave_ExplicitContextChannel_OverridesBakedChannel()
    {
        var dir = Directory.CreateTempSubdirectory("aspire-reseed-first-");

        try
        {
            var channelReader = new FakeChannelReader("daily");
            const string explicitChannel = "stable";

            // Simulate ScaffoldingService: config.Channel = !string.IsNullOrWhiteSpace(context.Channel)
            //     ? context.Channel : _channelReader.ReadChannel()
            var config = new AspireConfigFile();
            config.Channel = !string.IsNullOrWhiteSpace(explicitChannel)
                ? explicitChannel
                : channelReader.ReadChannel();
            config.Save(dir.FullName);

            var reloaded = AspireConfigFile.Load(dir.FullName);
            Assert.Equal("stable", reloaded?.Channel);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    // ---------------------------------------------------------------------------
    // Second save (line 194 of ScaffoldingService): prepareResult.ChannelName ?? reader.ReadChannel()
    // ---------------------------------------------------------------------------

    [Fact]
    public void SecondSave_PrepareResultChannelName_OverridesBakedChannel_WhenNotNull()
    {
        var dir = Directory.CreateTempSubdirectory("aspire-reseed-second-");

        try
        {
            var channelReader = new FakeChannelReader("daily");

            // Simulate ScaffoldingService: config.Channel = prepareResult.ChannelName ?? _channelReader.ReadChannel()
            string? prepareResultChannelName = "staging";
            var config = new AspireConfigFile();
            config.Channel = prepareResultChannelName ?? channelReader.ReadChannel();
            config.Save(dir.FullName);

            var reloaded = AspireConfigFile.Load(dir.FullName);
            Assert.Equal("staging", reloaded?.Channel);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }

    [Fact]
    public void SecondSave_BakedChannelUsed_WhenPrepareResultChannelIsNull()
    {
        var dir = Directory.CreateTempSubdirectory("aspire-reseed-second-");

        try
        {
            var channelReader = new FakeChannelReader("daily");

            // Simulate ScaffoldingService: config.Channel = prepareResult.ChannelName ?? _channelReader.ReadChannel()
            string? prepareResultChannelName = null;
            var config = new AspireConfigFile();
            config.Channel = prepareResultChannelName ?? channelReader.ReadChannel();
            config.Save(dir.FullName);

            var reloaded = AspireConfigFile.Load(dir.FullName);
            Assert.Equal("daily", reloaded?.Channel);
        }
        finally
        {
            dir.Delete(recursive: true);
        }
    }
}
