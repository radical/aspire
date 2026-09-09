"""Real unittest failures exported as a minimal TRX for collector coverage."""
import pathlib
import sys
import unittest
import uuid
import xml.etree.ElementTree as ET


class FailingTests(unittest.TestCase):
    def test_expected_value(self):
        self.assertEqual(1, 2)


result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromTestCase(FailingTests)
)
run = ET.Element("TestRun", xmlns="http://microsoft.com/schemas/VisualStudio/TeamTest/2010")
definitions = ET.SubElement(run, "TestDefinitions")
results = ET.SubElement(run, "Results")
for test, traceback in result.failures + result.errors:
    identity = str(uuid.uuid4())
    name = f"ShepherdFixture.FailingTests.{test._testMethodName}"
    definition = ET.SubElement(definitions, "UnitTest", name=name, id=identity)
    ET.SubElement(
        definition, "TestMethod",
        className="ShepherdFixture.FailingTests", name=test._testMethodName,
    )
    entry = ET.SubElement(results, "UnitTestResult", testId=identity, testName=name, outcome="Failed")
    error = ET.SubElement(ET.SubElement(entry, "Output"), "ErrorInfo")
    ET.SubElement(error, "Message").text = traceback
output = pathlib.Path("test-artifacts/ubuntu-latest/testresults/Fixture.Tests.trx")
output.parent.mkdir(parents=True, exist_ok=True)
ET.ElementTree(run).write(output, encoding="utf-8", xml_declaration=True)
sys.exit(not result.wasSuccessful())
