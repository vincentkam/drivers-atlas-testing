using System;

namespace workload_executor
{
    class Program
    {
        static bool __quit = false;

        static void Main(string[] args)
        {
            // Establish an event handler to process key press events and control break events (the Windows SIGINT)
            Console.CancelKeyPress += new ConsoleCancelEventHandler(ControlBreakEventHandler);

            var magicFileName = Environment.GetEnvironmentVariable("MAGIC_FILE_NAME");
            Console.WriteLine($"dotnet main> Magic: {magicFileName}");
            foreach (var arg in args) Console.WriteLine($"dotnet main> Arg: " + arg);

            // Loop until the magic file is created AND we've received a control break event
            while (true)
            {
                if (System.IO.File.Exists(magicFileName))
                {
                    Console.Write("dotnet main> Magic file detected. ");
                }

                if (System.IO.File.Exists(magicFileName) && __quit)
                {
                    // We never get here
                    Console.WriteLine($"\ndotnet main> Magic file exists and __quit was set to true by {nameof(ControlBreakEventHandler)}.");
                    break;
                }
            }
        }

        internal static void ControlBreakEventHandler(object sender, ConsoleCancelEventArgs args)
        {
            // We get here when astrolabe sends the control break event to the whole process group.
            Console.WriteLine($"\ndotnet int handler> The main program has been interrupted.");
            Console.WriteLine($"dotnet int handler>  Key pressed: {args.SpecialKey}");
            Console.WriteLine($"dotnet int handler>  Cancel property: {args.Cancel}");

            // Per the documentation example: https://docs.microsoft.com/en-us/dotnet/api/system.consolecanceleventargs.cancel
            // we set the Cancel property to true to prevent the process from terminating.
            Console.WriteLine("dotnet int handler> Setting the Cancel property to true...");
            args.Cancel = true;

            var timer = new System.Diagnostics.Stopwatch();
            Console.Write("dotnet int handler> Spinning until 4s have elapsed. Time (ms) elapsed: ");
            timer.Start();
            while (timer.ElapsedMilliseconds < 4000)
            {
                Console.Write(timer.ElapsedMilliseconds + " ");
                // This loop never finishes: Cygwin bash appears to terminate the process after circa 50-200ms
            }

            // Announce the new value of the Cancel property.
            Console.WriteLine($"dotnet int handler>   Cancel property: {args.Cancel}");
            Console.WriteLine($"dotnet int handler>   Setting {nameof(__quit)} to true.");
            Console.WriteLine($"dotnet int handler>   Main program will now resume...\n");
            __quit= true;
        }
    }

}
